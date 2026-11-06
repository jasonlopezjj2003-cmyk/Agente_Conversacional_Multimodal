#!/usr/bin/env python3
"""
Asistente de inventario con IA local (Ollama) que consulta tu base SQLite.

Funciona SIN internet una vez instalado el modelo. La IA no calcula ni adivina:
cada cifra sale de una consulta a la base de datos (inventario.db).

Preparación (una sola vez, con internet):
    1. Instala Ollama:            https://ollama.com
    2. Descarga un modelo:        ollama pull qwen3:8b
       (cualquier modelo con soporte de "tools"; ver --modelo)
    3. Instala la librería:       pip install ollama

Uso:
    python asistente_inventario.py                      # usa inventario.db y qwen3:8b
    python asistente_inventario.py --db otra.db --modelo llama3.1:8b
    python asistente_inventario.py --probar-herramientas   # prueba la base sin IA
"""

import argparse
import difflib
import json
import re
import sqlite3
import sys
import unicodedata
from pathlib import Path

MODELO_POR_DEFECTO = "qwen3:8b"
MAX_RONDAS = 6          # máximo de llamadas a herramientas por pregunta
MAX_MENSAJES = 30       # historial que se conserva en la conversación
LIMITE_LISTA = 30       # máximo de filas que se muestran en una tabla
MAX_FILAS_MODELO = 15   # filas de una lista que ve el modelo (el usuario ve la tabla completa)

INSTRUCCIONES = """Eres el asistente de inventario de un negocio. Respondes siempre en español.

REGLAS ESTRICTAS:
1. Todo dato sobre productos, cantidades, costos, ubicaciones o valores DEBE salir de las herramientas. Nunca inventes ni estimes cifras, y no hagas cálculos por tu cuenta: usa la herramienta que corresponda. Nunca digas que vas a usar una herramienta: llámala directamente, y nunca escribas marcadores como "X" en lugar de una cifra.
2. Si una búsqueda no devuelve resultados, dilo claramente ("no encontré ese producto") y sugiere buscar con otras palabras. No inventes productos.
3. Un mismo producto puede aparecer varias veces con códigos distintos porque corresponden a lotes diferentes. Si hay varios resultados, considéralos TODOS (no elijas uno solo) y, si parecen lotes distintos, indícalo.
4. Siempre indica la unidad de medida junto a cada cantidad (por ejemplo "24 Unidad" o "150 Metro").
5. Si el resultado dice que hay más resultados de los mostrados, menciónalo.
6. Si la pregunta es ambigua (por ejemplo, un nombre demasiado general), pide más detalle o muestra las opciones encontradas.
7. Cuando una consulta devuelve una lista de productos, el sistema la muestra automáticamente al usuario en una tabla (CODIGO, DESCRIPCION, UM, UBICACION, SALDO, COSTO, FAMILIA y OBSERVACIONES) justo debajo de tu mensaje. NUNCA escribas tablas ni enumeres los productos que ya están en esa tabla. Si el usuario solo pidió ver productos, no escribas nada más.
8. Si el usuario hace una pregunta que requiere análisis (cuál tiene más o menos saldo, costo o valor; cuántos hay; totales; comparaciones), responde en una a tres frases con la conclusión y las cifras clave, tomadas SOLO de las herramientas: usa resumen_busqueda para totales, conteos y extremos exactos, o buscar_productos con ordenar_por para rankings. Cita como máximo uno o dos productos (con su código) y siempre con su unidad de medida. Saldos en unidades de medida distintas no se pueden sumar ni comparar entre sí: indícalo si ocurre.
9. Si el usuario pide productos por marca, tipo de equipo o familia (por ejemplo "tractores John Deere", "llantas", "cabezal mack"), usa buscar_por_familia. Las abreviaturas del inventario (como JNDE = John Deere) las resuelve el sistema. Si no hay coincidencia exacta, el sistema muestra familias sugeridas: no inventes alternativas.
10. Sé breve y claro. No menciones las herramientas ni detalles técnicos.
"""


# --------------------------------------------------------------------------
# Familias y marcas: abreviaturas, plurales y errores de escritura
# --------------------------------------------------------------------------
# Cómo escribe la gente -> cómo aparece en el nombre de la familia en la base de datos
ALIAS_FAMILIA = {
    "john deere": "jnde", "jhon deere": "jnde", "jonh deere": "jnde", "jon deere": "jnde",
    "john dere": "jnde", "jhon dere": "jnde", "jonh dere": "jnde", "deere": "jnde", "dere": "jnde", "jd": "jnde",
    "newholland": "new holland", "nh": "new holland",
    "international": "inter", "internacional": "inter", "navistar": "inter",
    "cat": "caterpillar", "vanguard": "vanguad",
}
PALABRAS_VACIAS = {
    "de", "del", "la", "las", "el", "los", "un", "una", "unos", "unas", "para", "por", "con", "en", "y", "o", "a",
    "que", "hay", "tienen", "tiene", "tienes", "busco", "buscar", "buscando", "quiero", "necesito", "dame", "dime",
    "muestrame", "mostrar", "muestra", "lista", "listado", "producto", "productos", "repuesto", "repuestos",
    "marca", "marcas", "familia", "familias", "articulo", "articulos", "item", "items", "inventario", "favor",
    "john", "jhon", "jonh", "jon",
}


def _quitar_acentos(texto: str) -> str:
    return "".join(c for c in unicodedata.normalize("NFD", texto) if unicodedata.category(c) != "Mn")


def _singular(palabra: str) -> str:
    if palabra.endswith("es") and len(palabra) > 5:
        return palabra[:-2]
    if palabra.endswith("s") and len(palabra) > 3:
        return palabra[:-1]
    return palabra


def tokens_familia(texto: str) -> list:
    """Palabras de un nombre de familia, sin acentos ni plurales."""
    return [_singular(t) for t in re.findall(r"[a-z0-9]+", _quitar_acentos(str(texto)).lower())]


def tokens_consulta(texto: str) -> list:
    """Palabras que pidió el usuario, con alias aplicados (John Deere -> jnde), sin palabras vacías ni plurales."""
    t = _quitar_acentos(str(texto)).lower()
    for frase in sorted(ALIAS_FAMILIA, key=len, reverse=True):
        t = re.sub(rf"\b{re.escape(frase)}\b", ALIAS_FAMILIA[frase], t)
    return [_singular(x) for x in re.findall(r"[a-z0-9]+", t) if x not in PALABRAS_VACIAS]


def _coincide(q: str, f: str) -> bool:
    if q == f:
        return True
    # tolera errores de escritura solo en palabras largas (evita confundir "cat" con "case")
    return len(q) >= 5 and len(f) >= 5 and difflib.SequenceMatcher(None, q, f).ratio() >= 0.82


# --------------------------------------------------------------------------
# Acceso a la base de datos (solo lectura)
# --------------------------------------------------------------------------
class Inventario:
    # Columnas que devuelven todas las listas de productos
    _COLS = ("p.codigo, p.descripcion, p.unidad, p.ubicacion, p.familia, p.saldo, p.costo, "
             "ROUND(p.saldo * p.costo, 2) AS total, p.observaciones")

    def __init__(self, ruta_db: Path):
        if not ruta_db.exists():
            sys.exit(f"ERROR: no existe la base de datos {ruta_db}. "
                     "Ejecuta primero importar_inventario.py.")
        # mode=ro: la IA no puede modificar nada, aunque quisiera
        self.con = sqlite3.connect(f"{ruta_db.resolve().as_uri()}?mode=ro", uri=True,
                                    check_same_thread=False)  # el servidor web serializa las consultas
        self.con.row_factory = sqlite3.Row

    # -- utilidades ---------------------------------------------------------
    @staticmethod
    def _sin_acentos(texto: str) -> str:
        return "".join(c for c in unicodedata.normalize("NFD", texto)
                       if unicodedata.category(c) != "Mn")

    @staticmethod
    def _limite(valor, defecto, maximo=LIMITE_LISTA) -> int:
        try:
            return max(1, min(int(valor), maximo))
        except (TypeError, ValueError):
            return defecto

    @staticmethod
    def _filas(cursor):
        out = []
        for r in cursor:
            d = dict(r)
            for k in ("saldo", "costo", "total", "contado", "diferencia", "costo_diferencia"):
                if k in d and isinstance(d[k], float):
                    d[k] = round(d[k], 4 if k in ("saldo", "contado", "diferencia") else 2)
            out.append(d)
        return out

    def _familia_valida(self, familia):
        """Devuelve el nombre exacto de la familia (ignora mayúsculas/acentos) o None."""
        if not familia:
            return None
        objetivo = self._sin_acentos(str(familia)).lower().strip()
        for (f,) in self.con.execute("SELECT DISTINCT familia FROM productos"):
            if self._sin_acentos(f).lower() == objetivo:
                return f
        return "__NO_EXISTE__"

    def _familias(self):
        return [f for (f,) in self.con.execute("SELECT DISTINCT familia FROM productos ORDER BY 1")]

    # -- herramientas -------------------------------------------------------
    def buscar_productos(self, texto: str, limite: int = LIMITE_LISTA,
                         ordenar_por: str = "relevancia", orden: str = "desc"):
        palabras = re.findall(r"\w+", self._sin_acentos(str(texto)))
        if not palabras:
            return {"error": "Texto de búsqueda vacío."}
        limite = self._limite(limite, LIMITE_LISTA)
        col = {"saldo": "p.saldo", "costo": "p.costo", "total": "(p.saldo * p.costo)"}.get(str(ordenar_por).lower())
        direccion = "ASC" if str(orden).lower().startswith("asc") else "DESC"
        orden_fts = f"{col} {direccion}, rank" if col else "rank"
        orden_like = f"ORDER BY {col} {direccion}" if col else ""
        try:
            consulta = " AND ".join(f'"{p}"*' for p in palabras)
            filas = self._filas(self.con.execute(
                f"""SELECT {self._COLS}
                    FROM productos_fts f JOIN productos p ON p.codigo = f.codigo
                    WHERE productos_fts MATCH ? ORDER BY {orden_fts} LIMIT ?""",
                (consulta, limite + 1)))
        except sqlite3.OperationalError:   # sin FTS5
            cond = " AND ".join("descripcion LIKE ?" for _ in palabras)
            filas = self._filas(self.con.execute(
                f"SELECT {self._COLS} FROM productos p WHERE {cond} {orden_like} LIMIT ?",
                [f"%{p}%" for p in palabras] + [limite + 1]))
        if not filas:   # sin coincidencias en descripciones: probar por familia o marca
            por_familia = self.buscar_por_familia(texto, sugerir_todas=False)
            if por_familia:
                return por_familia
        hay_mas = len(filas) > limite
        return {"resultados": filas[:limite], "hay_mas_resultados": hay_mas,
                "nota": "Resultados incompletos: pide al usuario más detalle." if hay_mas else ""}

    # -- familias y marcas --------------------------------------------------
    def _familias_con_conteo(self):
        return [(f, n) for f, n in self.con.execute(
            "SELECT familia, COUNT(*) FROM productos GROUP BY familia ORDER BY familia")]

    @staticmethod
    def _sugerencias(items, mensaje):
        return {"sugerencias": [{"familia": f, "productos": n} for f, n, *_ in items], "mensaje_usuario": mensaje}

    def _productos_de_familias(self, familias):
        marcas = ",".join("?" for _ in familias)
        total = self.con.execute(f"SELECT COUNT(*) FROM productos WHERE familia IN ({marcas})", familias).fetchone()[0]
        filas = self._filas(self.con.execute(
            f"SELECT {self._COLS} FROM productos p WHERE p.familia IN ({marcas}) "
            f"ORDER BY p.descripcion LIMIT ?", familias + [LIMITE_LISTA]))
        return {"titulo": "Familia: " + ", ".join(familias), "total_coincidencias": total,
                "mostrando": len(filas), "hay_mas_resultados": total > len(filas), "resultados": filas}

    def productos_de_familia(self, familia: str):
        """Productos de una familia por su nombre exacto (la usan los botones de sugerencia)."""
        fam = self._familia_valida(familia)
        if not fam or fam == "__NO_EXISTE__":
            return {"error": f"La familia '{familia}' no existe.", "familias_disponibles": self._familias()}
        return self._productos_de_familias([fam])

    def buscar_por_familia(self, texto: str, sugerir_todas: bool = True):
        """Busca por marca, tipo de equipo o familia. Si no hay coincidencia exacta, sugiere familias parecidas."""
        q = tokens_consulta(texto)
        if not q:
            return self._sugerencias(self._familias_con_conteo(), "Estas son las familias disponibles:")
        directas, parciales = [], []
        for fam, n in self._familias_con_conteo():
            tf = tokens_familia(fam)
            m = sum(1 for tq in q if any(_coincide(tq, x) for x in tf))
            if m == len(q):
                directas.append((fam, n))
            elif m > 0:
                parciales.append((fam, n, m))
        if len(directas) == 1:
            resultado = self._productos_de_familias([directas[0][0]])
            resultado["nota"] = f"Coincidencia por familia: '{texto}' corresponde a la familia {directas[0][0]}."
            return resultado
        if len(directas) > 1:
            directas.sort(key=lambda x: -x[1])
            return self._sugerencias(directas[:12], f"Hay varias familias relacionadas con «{texto}». ¿Cuál buscabas?")
        if parciales:
            parciales.sort(key=lambda x: (-x[2], -x[1]))
            return self._sugerencias(parciales[:8], f"No encontré «{texto}» exactamente. ¿Buscabas alguna de estas familias?")
        if sugerir_todas:
            return self._sugerencias(self._familias_con_conteo(), f"No encontré «{texto}». Estas son las familias disponibles:")
        return None

    def buscar_por_descripcion_foto(self, palabras, textos=()):
        """Productos que mejor coinciden con las palabras clave (y textos legibles) sacados de una foto.
        Cada palabra que aparece en la descripción suma un punto; un código o texto exacto suma cinco."""
        claves = []
        for p in palabras or []:
            for t in re.findall(r"[a-z0-9]+", self._sin_acentos(str(p)).lower()):
                t = _singular(t)
                if len(t) >= 3 and t not in PALABRAS_VACIAS and t not in claves:
                    claves.append(t)
        codigos = []
        for x in textos or []:
            for t in re.findall(r"[A-Za-z0-9][A-Za-z0-9\-/.]{3,}", str(x)):
                t = t.strip(".-/").upper()
                if len(t) >= 4 and t not in codigos:
                    codigos.append(t)
        claves, codigos = claves[:8], codigos[:5]
        if not claves and not codigos:
            return {"resultados": [], "claves": [], "codigos": []}

        puntaje, filas_por_codigo = {}, {}
        if claves:
            try:
                consulta = " OR ".join(f'"{c}"*' for c in claves)
                filas = self.con.execute(
                    f"SELECT {self._COLS} FROM productos_fts f JOIN productos p ON p.codigo = f.codigo "
                    f"WHERE productos_fts MATCH ? ORDER BY rank LIMIT 300", (consulta,)).fetchall()
            except sqlite3.OperationalError:   # sin FTS5
                cond = " OR ".join("p.descripcion LIKE ?" for _ in claves)
                filas = self.con.execute(
                    f"SELECT {self._COLS} FROM productos p WHERE {cond} LIMIT 300",
                    [f"%{c}%" for c in claves]).fetchall()
            for orden, r in enumerate(filas):
                desc = self._sin_acentos(r["descripcion"]).lower()
                puntaje[r["codigo"]] = (sum(1 for c in claves if c in desc), -orden)
                filas_por_codigo[r["codigo"]] = r
        for cod in codigos:
            for r in self.con.execute(
                    f"SELECT {self._COLS} FROM productos p WHERE p.codigo = ? OR UPPER(p.descripcion) LIKE ? LIMIT 20",
                    (cod, f"%{cod}%")):
                n, o = puntaje.get(r["codigo"], (0, 0))
                puntaje[r["codigo"]] = (n + 5, o)
                filas_por_codigo[r["codigo"]] = r

        ordenados = sorted(puntaje, key=lambda k: puntaje[k], reverse=True)[:LIMITE_LISTA]
        filas = self._filas([filas_por_codigo[k] for k in ordenados])
        return {"titulo": "Posibles coincidencias con la foto", "total_coincidencias": len(puntaje),
                "mostrando": len(filas), "hay_mas_resultados": len(puntaje) > len(filas),
                "resultados": filas, "claves": claves, "codigos": codigos}

    def _coincidencias(self, palabras):
        """CTE con los productos que contienen todas las palabras (y sus parámetros)."""
        try:
            self.con.execute("SELECT 1 FROM productos_fts LIMIT 1")
            consulta = " AND ".join(f'"{p}"*' for p in palabras)
            return ("WITH m AS (SELECT p.* FROM productos_fts f JOIN productos p ON p.codigo = f.codigo "
                    "WHERE productos_fts MATCH ?)", [consulta])
        except sqlite3.OperationalError:
            cond = " AND ".join("descripcion LIKE ?" for _ in palabras)
            return (f"WITH m AS (SELECT * FROM productos WHERE {cond})", [f"%{p}%" for p in palabras])

    def resumen_busqueda(self, texto: str):
        palabras = re.findall(r"\w+", self._sin_acentos(str(texto)))
        if not palabras:
            return {"error": "Texto de búsqueda vacío."}
        cte, params = self._coincidencias(palabras)
        general = self._filas(self.con.execute(
            f"{cte} SELECT COUNT(*) AS productos, ROUND(SUM(saldo * costo), 2) AS valor_total, "
            f"SUM(saldo = 0) AS sin_stock FROM m", params))[0]
        if not general["productos"]:
            return {"error": f"No encontré productos que coincidan con '{texto}'."}
        por_unidad = self._filas(self.con.execute(
            f"{cte} SELECT unidad, COUNT(*) AS productos, ROUND(SUM(saldo), 4) AS saldo_total "
            f"FROM m GROUP BY unidad ORDER BY productos DESC", params))

        def top(orden, donde=""):
            return self._filas(self.con.execute(
                f"{cte} SELECT codigo, descripcion, unidad, saldo, costo, "
                f"ROUND(saldo * costo, 2) AS total FROM m {donde} ORDER BY {orden} LIMIT 2", params))

        resultado = {"texto_buscado": texto, **general, "por_unidad": por_unidad,
                     "mayor_saldo": top("saldo DESC"),
                     "menor_saldo_con_stock": top("saldo ASC", "WHERE saldo > 0"),
                     "mayor_costo": top("costo DESC"),
                     "menor_costo_mayor_que_cero": top("costo ASC", "WHERE costo > 0"),
                     "mayor_valor": top("saldo * costo DESC")}
        if len(por_unidad) > 1:
            resultado["nota"] = ("Hay unidades de medida distintas: los saldos no se pueden sumar ni "
                                 "comparar entre unidades; usa por_unidad.")
        return resultado

    def obtener_producto(self, codigo: str):
        filas = self._filas(self.con.execute(
            f"SELECT {self._COLS} FROM productos p WHERE p.codigo = ?", (str(codigo).strip(),)))
        if not filas:
            return {"error": f"No existe un producto con código {codigo}."}
        conteos = self._filas(self.con.execute(
            "SELECT fecha, cantidad FROM conteos WHERE codigo = ? ORDER BY id DESC", (str(codigo).strip(),)))
        return {"producto": filas[0], "conteos": conteos}

    def stock_bajo(self, umbral: float, familia: str = None, limite: int = LIMITE_LISTA):
        fam = self._familia_valida(familia)
        if fam == "__NO_EXISTE__":
            return {"error": f"La familia '{familia}' no existe.", "familias_disponibles": self._familias()}
        limite = self._limite(limite, LIMITE_LISTA)
        try:
            umbral = float(umbral)
        except (TypeError, ValueError):
            return {"error": "El umbral debe ser un número."}
        filtro, params = "saldo <= ?", [umbral]
        if fam:
            filtro += " AND familia = ?"
            params.append(fam)
        total = self.con.execute(f"SELECT COUNT(*) FROM productos WHERE {filtro}", params).fetchone()[0]
        filas = self._filas(self.con.execute(
            f"SELECT {self._COLS} FROM productos p "
            f"WHERE {filtro} ORDER BY saldo ASC, descripcion LIMIT ?", params + [limite]))
        return {"total_coincidencias": total, "mostrando": len(filas), "resultados": filas}

    def valor_inventario(self, familia: str = None):
        fam = self._familia_valida(familia)
        if fam == "__NO_EXISTE__":
            return {"error": f"La familia '{familia}' no existe.", "familias_disponibles": self._familias()}
        filtro, params = ("WHERE familia = ?", [fam]) if fam else ("", [])
        general = self._filas(self.con.execute(
            f"SELECT COUNT(*) AS productos, SUM(saldo > 0) AS con_stock, SUM(saldo <= 0) AS sin_stock, "
            f"ROUND(SUM(total), 2) AS valor_total FROM v_inventario {filtro}", params))[0]
        por_unidad = self._filas(self.con.execute(
            f"SELECT unidad, COUNT(*) AS productos, ROUND(SUM(saldo), 2) AS saldo_total "
            f"FROM v_inventario {filtro} GROUP BY unidad ORDER BY productos DESC", params))
        por_familia = self._filas(self.con.execute(
            f"SELECT familia, COUNT(*) AS productos, ROUND(SUM(total), 2) AS valor_total "
            f"FROM v_inventario {filtro} GROUP BY familia ORDER BY valor_total DESC", params))
        return {"filtro_familia": fam, **general, "por_unidad": por_unidad, "por_familia": por_familia,
                "nota": "'productos' cuenta códigos distintos, no piezas; los saldos de unidades de medida "
                        "distintas no se suman entre sí."}

    def productos_al_azar(self, cantidad: int = 10, familia: str = None):
        fam = self._familia_valida(familia)
        if fam == "__NO_EXISTE__":
            return {"error": f"La familia '{familia}' no existe.", "familias_disponibles": self._familias()}
        cantidad = self._limite(cantidad, 10)
        filtro, params = ("WHERE p.familia = ?", [fam]) if fam else ("", [])
        filas = self._filas(self.con.execute(
            f"SELECT {self._COLS} FROM productos p {filtro} ORDER BY RANDOM() LIMIT ?", params + [cantidad]))
        return {"titulo": "Productos al azar" + (f" · {fam}" if fam else ""), "resultados": filas}

    def productos_en_ubicacion(self, ubicacion: str, limite: int = LIMITE_LISTA):
        limite = self._limite(limite, LIMITE_LISTA)
        patron = str(ubicacion).strip()
        total = self.con.execute(
            "SELECT COUNT(*) FROM productos WHERE ubicacion LIKE ?", (f"{patron}%",)).fetchone()[0]
        filas = self._filas(self.con.execute(
            f"SELECT {self._COLS} FROM productos p "
            "WHERE ubicacion LIKE ? ORDER BY ubicacion, descripcion LIMIT ?", (f"{patron}%", limite)))
        if not filas:
            return {"error": f"No hay productos en la ubicación '{ubicacion}'."}
        return {"total_coincidencias": total, "mostrando": len(filas), "resultados": filas}

    def diferencias_de_conteo(self, familia: str = None, limite: int = LIMITE_LISTA):
        fam = self._familia_valida(familia)
        if fam == "__NO_EXISTE__":
            return {"error": f"La familia '{familia}' no existe.", "familias_disponibles": self._familias()}
        limite = self._limite(limite, LIMITE_LISTA)
        filtro, params = ("WHERE familia = ?", [fam]) if fam else ("", [])
        resumen = self._filas(self.con.execute(
            f"SELECT COUNT(*) AS items_con_diferencia, ROUND(SUM(costo_diferencia), 2) AS costo_neto "
            f"FROM v_diferencias {filtro}", params))[0]
        filas = self._filas(self.con.execute(
            f"SELECT d.codigo, d.descripcion, d.unidad, d.ubicacion, d.familia, d.saldo, p.costo, "
            f"p.observaciones, d.contado, d.fecha_conteo, d.diferencia, d.costo_diferencia "
            f"FROM v_diferencias d JOIN productos p ON p.codigo = d.codigo "
            f"{filtro.replace('familia', 'd.familia')} "
            f"ORDER BY ABS(d.costo_diferencia) DESC LIMIT ?", params + [limite]))
        return {**resumen, "nota": "Ordenado por mayor impacto en costo (valor absoluto).", "resultados": filas}

    def listar_familias(self):
        familias = self._familias_con_conteo()
        return self._sugerencias(familias, f"Hay {len(familias)} familias (categorías) de productos. Elige una para ver sus productos:")


# --------------------------------------------------------------------------
# Definición de herramientas para el modelo
# --------------------------------------------------------------------------
def _herr(nombre, descripcion, propiedades, requeridos=()):
    return {"type": "function", "function": {
        "name": nombre, "description": descripcion,
        "parameters": {"type": "object", "properties": propiedades, "required": list(requeridos)}}}


HERRAMIENTAS = [
    _herr("buscar_productos",
          "Busca productos por nombre, medida o código de fabricante (ej. 'arandela 55', 'manguera 3/8'). "
          "Devuelve código, descripción, unidad, ubicación, saldo, costo y valor total. "
          "Úsala siempre que el usuario pregunte por un producto o por cuánto stock hay de algo. "
          "Para rankings ('cuál tiene más saldo', 'los más caros') usa ordenar_por y orden.",
          {"texto": {"type": "string", "description": "Palabras a buscar en la descripción."},
           "ordenar_por": {"type": "string", "enum": ["relevancia", "saldo", "costo", "total"],
                           "description": "Criterio de orden (por defecto relevancia). 'total' = saldo x costo."},
           "orden": {"type": "string", "enum": ["desc", "asc"],
                     "description": "desc = de mayor a menor (por defecto); asc = de menor a mayor."}},
          ["texto"]),
    _herr("buscar_por_familia",
          "Busca productos por marca, tipo de equipo o familia (ej. 'tractores John Deere', 'cabezal mack', 'llantas'). "
          "Entiende abreviaturas del inventario (JNDE = John Deere), plurales y errores de escritura. Si hay una sola "
          "familia que coincide, muestra sus productos; si hay varias o ninguna exacta, muestra familias sugeridas "
          "para que el usuario elija. Úsala cuando el usuario pida por marca, tipo de equipo o familia.",
          {"texto": {"type": "string", "description": "Marca, equipo o familia, tal como la escribió el usuario."}},
          ["texto"]),
    _herr("resumen_busqueda",
          "Calcula totales y extremos EXACTOS de todos los productos que coinciden con un texto: cuántos son, "
          "valor total, cuántos sin stock, saldo total por unidad de medida y los 2 de mayor/menor saldo, costo y valor. "
          "Úsala solo cuando hay un producto o texto concreto ('cuántas mangueras hay', 'cuál manguera tiene más saldo'). "
          "Para totales generales del inventario usa valor_inventario.",
          {"texto": {"type": "string", "description": "Palabras a buscar en la descripción."}},
          ["texto"]),
    _herr("obtener_producto",
          "Devuelve el detalle de UN producto a partir de su código exacto, incluyendo su historial de conteos.",
          {"codigo": {"type": "string", "description": "Código exacto del producto."}}, ["codigo"]),
    _herr("stock_bajo",
          "Lista productos cuyo saldo es menor o igual a un umbral, opcionalmente de una familia.",
          {"umbral": {"type": "number", "description": "Saldo máximo a considerar."},
           "familia": {"type": "string", "description": "Familia opcional (ver listar_familias)."},
           "limite": {"type": "integer", "description": "Usa solo si el usuario pide expresamente MENOS resultados (máximo 30). Por defecto se muestran hasta 30."}},
          ["umbral"]),
    _herr("valor_inventario",
          "Totales generales del inventario: cantidad de productos (códigos), cuántos tienen o no stock, valor total "
          "(saldo x costo) y saldo total por unidad de medida, en total y por familia. Úsala para 'cuántos productos "
          "hay', 'cuántas piezas hay en stock', 'valor total' o 'total del inventario' (sin un producto concreto).",
          {"familia": {"type": "string", "description": "Familia opcional para filtrar."}}),
    _herr("productos_al_azar",
          "Muestra productos elegidos al azar (opcionalmente de una familia). Úsala cuando el usuario pida una lista "
          "aleatoria, al azar o 'cualquier' producto.",
          {"cantidad": {"type": "integer", "description": "Cuántos mostrar (por defecto 10, máximo 30)."},
           "familia": {"type": "string", "description": "Familia opcional."}}),
    _herr("productos_en_ubicacion",
          "Lista los productos de una ubicación (acepta prefijo, ej. '643' trae toda la zona 643-xxx).",
          {"ubicacion": {"type": "string", "description": "Ubicación o prefijo, ej. '643-008'."},
           "limite": {"type": "integer", "description": "Usa solo si el usuario pide expresamente MENOS resultados (máximo 30). Por defecto se muestran hasta 30."}},
          ["ubicacion"]),
    _herr("diferencias_de_conteo",
          "Muestra los productos donde el conteo físico no coincide con el saldo del sistema, "
          "con la diferencia en cantidad y en costo. Úsala para preguntas sobre faltantes, sobrantes o diferencias.",
          {"familia": {"type": "string", "description": "Familia opcional para filtrar."},
           "limite": {"type": "integer", "description": "Usa solo si el usuario pide expresamente MENOS resultados (máximo 30). Por defecto se muestran hasta 30."}}),
    _herr("listar_familias", "Muestra las familias (categorías) de productos disponibles, con su cantidad de productos.", {}),
]


def ejecutar_herramienta(inv: Inventario, nombre: str, args: dict):
    funcion = getattr(inv, nombre, None)
    if nombre not in {h["function"]["name"] for h in HERRAMIENTAS} or funcion is None:
        return {"error": f"La herramienta '{nombre}' no existe."}
    if nombre == "buscar_productos":
        args = {k: v for k, v in args.items() if k != "limite"}   # las búsquedas siempre muestran hasta 30
    try:
        return funcion(**args)
    except TypeError as e:
        return {"error": f"Argumentos inválidos para {nombre}: {e}"}
    except sqlite3.Error as e:
        return {"error": f"Error de base de datos: {e}"}


# --------------------------------------------------------------------------
# Tablas de productos (las muestra la interfaz; el modelo solo recibe un resumen)
# --------------------------------------------------------------------------
COLUMNAS_TABLA = [("codigo", "CODIGO"), ("descripcion", "DESCRIPCION"), ("unidad", "UM"),
                  ("ubicacion", "UBICACION"), ("saldo", "SALDO"), ("costo", "COSTO"),
                  ("familia", "FAMILIA"), ("observaciones", "OBSERVACIONES")]
COLUMNAS_EXTRA = [("contado", "CONTADO"), ("diferencia", "DIFERENCIA"), ("costo_diferencia", "COSTO DIF.")]


def extraer_tabla(resultado):
    """Si el resultado de una herramienta es una lista de productos, la devuelve para mostrarla como tabla."""
    if not isinstance(resultado, dict):
        return None
    filas = resultado.get("resultados")
    if filas is None and isinstance(resultado.get("producto"), dict):
        filas = [resultado["producto"]]
    if not filas:
        return None
    return {"filas": filas, "total": resultado.get("total_coincidencias"),
            "hay_mas": bool(resultado.get("hay_mas_resultados")), "titulo": resultado.get("titulo")}


def compactar_para_modelo(resultado):
    """El modelo trabaja con un contexto pequeño: recibe pocas filas y sin campos vacíos."""
    if not isinstance(resultado, dict) or "resultados" not in resultado:
        return resultado
    filas = resultado["resultados"]
    salida = {k: v for k, v in resultado.items() if k != "resultados"}
    salida["resultados"] = [{k: v for k, v in f.items() if v not in (None, "")}
                            for f in filas[:MAX_FILAS_MODELO]]
    if filas:
        aviso = "La tabla completa de estos productos ya se muestra al usuario; no la repitas."
        if len(filas) > MAX_FILAS_MODELO:
            aviso += f" Aquí solo ves las primeras {MAX_FILAS_MODELO} filas."
        salida["aviso"] = aviso
    return salida


_ANALISIS = re.compile(r"\b(cual(es)?|cuant[oa]s?|total(es)?|suma|sumar|promedio|mayor(es)?|menor(es)?|"
                       r"maxim[oa]|minim[oa]|mas|menos|compar\w*|ranking|top|ordena\w*|resumen|analiz\w*)\b")


def ultima_pregunta(mensajes) -> str:
    for m in reversed(mensajes):
        rol = m["role"] if isinstance(m, dict) else m.role
        if rol == "user":
            return (m["content"] if isinstance(m, dict) else m.content) or ""
    return ""


def pregunta_analitica(texto: str) -> bool:
    """True si la pregunta pide un análisis (cuál es mayor, cuántos hay, totales...) y no solo ver productos."""
    return bool(_ANALISIS.search(Inventario._sin_acentos(texto or "").lower()))


_PROMESA = re.compile(r"\b(utiliz|usar[eé]|usare|voy a|consultar[eé]|funci[oó]n|herramienta|ejecut)", re.I)


def titulo_tabla(nombre: str, args: dict) -> str:
    if nombre == "buscar_productos":
        return f"Búsqueda: {args.get('texto', '')}"
    if nombre == "buscar_por_familia":
        return f"Familia: {args.get('texto', '')}"
    if nombre == "productos_al_azar":
        return "Productos al azar"
    if nombre == "stock_bajo":
        return f"Stock menor o igual a {args.get('umbral', '')}" + (f" · {args['familia']}" if args.get("familia") else "")
    if nombre == "productos_en_ubicacion":
        return f"Ubicación: {args.get('ubicacion', '')}"
    if nombre == "diferencias_de_conteo":
        return "Diferencias de conteo" + (f" · {args['familia']}" if args.get("familia") else "")
    if nombre == "obtener_producto":
        return f"Producto {args.get('codigo', '')}"
    return nombre


def tabla_texto(filas) -> str:
    """Tabla en texto plano para el modo consola."""
    cols = COLUMNAS_TABLA + (COLUMNAS_EXTRA if filas and "diferencia" in filas[0] else [])
    datos = [[str("" if f.get(k) is None else f.get(k))[:40] for k, _ in cols] for f in filas]
    anchos = [max(len(t), *(len(d[i]) for d in datos)) for i, (_, t) in enumerate(cols)]
    linea = lambda celdas: "  ".join(c.ljust(w) for c, w in zip(celdas, anchos))
    return "\n".join([linea([t for _, t in cols]), "  ".join("-" * w for w in anchos)] + [linea(d) for d in datos])


# --------------------------------------------------------------------------
# Conversación con el modelo
# --------------------------------------------------------------------------
def responder(ollama, inv: Inventario, modelo: str, mensajes: list, verbose: bool = True,
              registro: list = None, tablas: list = None, solo_tabla: bool = False,
              sugerencias: list = None) -> str:
    """Con solo_tabla=True, si todas las consultas devolvieron listas de productos y la pregunta solo pide
    verlos, la respuesta es únicamente la tabla (texto vacío) y se evita una segunda llamada al modelo.
    Si la pregunta pide un análisis (cuál tiene más, cuántos hay...), el modelo además comenta el resultado."""
    herramientas_usadas = False
    reintentado = False
    aviso = []
    for _ in range(MAX_RONDAS):
        resp = ollama.chat(model=modelo, messages=mensajes + aviso, tools=HERRAMIENTAS,
                           options={"temperature": 0})
        msg = resp.message
        if not msg.tool_calls and not herramientas_usadas and not reintentado:
            texto = (msg.content or "").strip()
            if not texto or _PROMESA.search(texto):
                # El modelo anunció que usaría una herramienta (o no dijo nada) sin llamarla: se le pide que la llame.
                reintentado = True
                aviso = [{"role": "user", "content": "Responde llamando a la herramienta adecuada; no escribas que la vas a usar ni inventes cifras."}]
                continue
        aviso = []
        mensajes.append(msg)
        if not msg.tool_calls:
            texto = (msg.content or "").strip()
            if not texto or (not herramientas_usadas and _PROMESA.search(texto)):
                return "No pude consultar el inventario con esa pregunta. Prueba a reformularla, por ejemplo indicando el producto o la familia."
            return texto
        herramientas_usadas = True
        todas_con_tabla = True
        mensaje_sugerencia = None
        for llamada in msg.tool_calls:
            nombre = llamada.function.name
            args = dict(llamada.function.arguments or {})
            if verbose:
                print(f"   [consulta: {nombre}({json.dumps(args, ensure_ascii=False)})]")
            if registro is not None:
                registro.append({"herramienta": nombre, "argumentos": args})
            resultado = ejecutar_herramienta(inv, nombre, args)
            if isinstance(resultado, dict) and resultado.get("sugerencias") is not None:
                if sugerencias is not None:
                    sugerencias.extend(resultado["sugerencias"])
                mensaje_sugerencia = resultado.get("mensaje_usuario") or mensaje_sugerencia
            tabla = extraer_tabla(resultado)
            if tabla:
                if tablas is not None:
                    tabla["titulo"] = tabla.get("titulo") or titulo_tabla(nombre, args)
                    tablas.append(tabla)
            else:
                todas_con_tabla = False
            mensajes.append({"role": "tool", "tool_name": nombre,
                             "content": json.dumps(compactar_para_modelo(resultado),
                                                   ensure_ascii=False, separators=(",", ":"))})
        if solo_tabla and sugerencias is not None and mensaje_sugerencia:
            # Texto fijo (no lo redacta el modelo): el usuario elige una de las familias sugeridas
            nombres = ", ".join(sg["familia"] for sg in sugerencias[:10])
            mensajes.append({"role": "assistant", "content": f"{mensaje_sugerencia} (Opciones mostradas: {nombres})"})
            return mensaje_sugerencia
        if solo_tabla and tablas is not None and todas_con_tabla and not pregunta_analitica(ultima_pregunta(mensajes)):
            # Se deja constancia en el historial para que las preguntas siguientes tengan contexto
            mensajes.append({"role": "assistant", "content": "(Se mostró al usuario la tabla con los resultados.)"})
            return ""
    return "No logré completar la consulta. Prueba reformular la pregunta."


def modo_chat(inv: Inventario, modelo: str, verbose: bool):
    try:
        import ollama
    except ImportError:
        sys.exit("Falta la librería de Ollama. Instálala con:  pip install ollama")

    mensajes = [{"role": "system", "content": INSTRUCCIONES}]
    print(f"Asistente de inventario (modelo: {modelo}). Escribe 'salir' para terminar.\n")
    while True:
        try:
            pregunta = input("Tú: ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            break
        if not pregunta:
            continue
        if pregunta.lower() in {"salir", "exit", "quit"}:
            break
        mensajes.append({"role": "user", "content": pregunta})
        try:
            tablas = []
            sugs = []
            respuesta = responder(ollama, inv, modelo, mensajes, verbose, tablas=tablas, solo_tabla=True, sugerencias=sugs)
        except Exception as e:  # ollama apagado, modelo no descargado, etc.
            mensajes.pop()
            print(f"\nError al hablar con Ollama: {e}\n"
                  f"Revisa que Ollama esté abierto y que el modelo exista:  ollama pull {modelo}\n")
            continue
        print(f"\nAsistente: {respuesta}\n" if respuesta else "")
        for tabla in tablas:
            print(tabla_texto(tabla["filas"]) + "\n")
        for sg in sugs:
            print(f"  - {sg['familia']} ({sg['productos']})")
        # Conserva el sistema + los mensajes más recientes
        if len(mensajes) > MAX_MENSAJES:
            mensajes = [mensajes[0]] + mensajes[-(MAX_MENSAJES - 1):]


def modo_probar(inv: Inventario):
    """Ejecuta las herramientas directamente, sin IA, para verificar la base."""
    pruebas = [
        ("listar_familias", {}),
        ("valor_inventario", {}),
        ("buscar_productos", {"texto": "arandela 55"}),
        ("stock_bajo", {"umbral": 2, "limite": 3}),
        ("diferencias_de_conteo", {"limite": 3}),
    ]
    for nombre, args in pruebas:
        print(f"\n== {nombre}({args})")
        print(json.dumps(ejecutar_herramienta(inv, nombre, args), ensure_ascii=False, indent=2)[:1500])


def main():
    ap = argparse.ArgumentParser(description="Asistente de inventario con IA local.")
    ap.add_argument("--db", type=Path, default=Path("inventario.db"))
    ap.add_argument("--modelo", default=MODELO_POR_DEFECTO, help="Modelo de Ollama con soporte de tools")
    ap.add_argument("--silencioso", action="store_true", help="No mostrar las consultas que hace la IA")
    ap.add_argument("--probar-herramientas", action="store_true", help="Probar la base sin usar IA")
    args = ap.parse_args()

    inv = Inventario(args.db)
    if args.probar_herramientas:
        modo_probar(inv)
    else:
        modo_chat(inv, args.modelo, verbose=not args.silencioso)


if __name__ == "__main__":
    main()
