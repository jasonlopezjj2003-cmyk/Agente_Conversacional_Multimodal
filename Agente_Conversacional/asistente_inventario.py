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
1. Todo dato sobre productos, cantidades, costos, ubicaciones o valores DEBE salir de las herramientas. Nunca inventes ni estimes cifras, y no hagas cálculos por tu cuenta: usa la herramienta que corresponda.
2. Si una búsqueda no devuelve resultados, dilo claramente ("no encontré ese producto") y sugiere buscar con otras palabras. No inventes productos.
3. Un mismo producto puede aparecer varias veces con códigos distintos porque corresponden a lotes diferentes. Si hay varios resultados, considéralos TODOS (no elijas uno solo) y, si parecen lotes distintos, indícalo.
4. Siempre indica la unidad de medida junto a cada cantidad (por ejemplo "24 Unidad" o "150 Metro").
5. Si el resultado dice que hay más resultados de los mostrados, menciónalo.
6. Si la pregunta es ambigua (por ejemplo, un nombre demasiado general), pide más detalle o muestra las opciones encontradas.
7. Cuando una consulta devuelve una lista de productos, el sistema la muestra automáticamente al usuario en una tabla (CODIGO, DESCRIPCION, UM, UBICACION, SALDO, COSTO, FAMILIA y OBSERVACIONES) justo debajo de tu mensaje. NUNCA escribas tablas ni enumeres los productos que ya están en esa tabla. Si el usuario solo pidió ver productos, no escribas nada más.
8. Si el usuario hace una pregunta que requiere análisis (cuál tiene más o menos saldo, costo o valor; cuántos hay; totales; comparaciones), responde en una a tres frases con la conclusión y las cifras clave, tomadas SOLO de las herramientas: usa resumen_busqueda para totales, conteos y extremos exactos, o buscar_productos con ordenar_por para rankings. Cita como máximo uno o dos productos (con su código) y siempre con su unidad de medida. Saldos en unidades de medida distintas no se pueden sumar ni comparar entre sí: indícalo si ocurre.
9. Sé breve y claro. No menciones las herramientas ni detalles técnicos.
"""


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
        hay_mas = len(filas) > limite
        return {"resultados": filas[:limite], "hay_mas_resultados": hay_mas,
                "nota": "Resultados incompletos: pide al usuario más detalle." if hay_mas else ""}

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
            f"SELECT COUNT(*) AS productos, ROUND(SUM(total), 2) AS valor_total FROM v_inventario {filtro}", params))[0]
        por_familia = self._filas(self.con.execute(
            f"SELECT familia, COUNT(*) AS productos, ROUND(SUM(total), 2) AS valor_total "
            f"FROM v_inventario {filtro} GROUP BY familia ORDER BY valor_total DESC", params))
        return {"filtro_familia": fam, **general, "por_familia": por_familia}

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
        return {"familias": self._familias()}


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
    _herr("resumen_busqueda",
          "Calcula totales y extremos EXACTOS de todos los productos que coinciden con un texto: cuántos son, "
          "valor total, cuántos sin stock, saldo total por unidad de medida y los 2 de mayor/menor saldo, costo y valor. "
          "Úsala para preguntas de 'cuántos hay', 'total', 'cuál tiene más/menos' o comparaciones.",
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
          "Calcula el valor total del inventario (saldo x costo) y la cantidad de productos, "
          "en total y por familia. Úsala para cualquier pregunta sobre valor, total o cuántos productos hay.",
          {"familia": {"type": "string", "description": "Familia opcional para filtrar."}}),
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
    _herr("listar_familias", "Lista las familias (categorías) de productos disponibles.", {}),
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
            "hay_mas": bool(resultado.get("hay_mas_resultados"))}


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


def titulo_tabla(nombre: str, args: dict) -> str:
    if nombre == "buscar_productos":
        return f"Búsqueda: {args.get('texto', '')}"
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
              registro: list = None, tablas: list = None, solo_tabla: bool = False) -> str:
    """Con solo_tabla=True, si todas las consultas devolvieron listas de productos y la pregunta solo pide
    verlos, la respuesta es únicamente la tabla (texto vacío) y se evita una segunda llamada al modelo.
    Si la pregunta pide un análisis (cuál tiene más, cuántos hay...), el modelo además comenta el resultado."""
    for _ in range(MAX_RONDAS):
        resp = ollama.chat(model=modelo, messages=mensajes, tools=HERRAMIENTAS,
                           options={"temperature": 0})
        msg = resp.message
        mensajes.append(msg)
        if not msg.tool_calls:
            return (msg.content or "").strip() or "No pude generar una respuesta."
        todas_con_tabla = True
        for llamada in msg.tool_calls:
            nombre = llamada.function.name
            args = dict(llamada.function.arguments or {})
            if verbose:
                print(f"   [consulta: {nombre}({json.dumps(args, ensure_ascii=False)})]")
            if registro is not None:
                registro.append({"herramienta": nombre, "argumentos": args})
            resultado = ejecutar_herramienta(inv, nombre, args)
            tabla = extraer_tabla(resultado)
            if tabla:
                if tablas is not None:
                    tabla["titulo"] = titulo_tabla(nombre, args)
                    tablas.append(tabla)
            else:
                todas_con_tabla = False
            mensajes.append({"role": "tool", "tool_name": nombre,
                             "content": json.dumps(compactar_para_modelo(resultado),
                                                   ensure_ascii=False, separators=(",", ":"))})
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
            respuesta = responder(ollama, inv, modelo, mensajes, verbose, tablas=tablas, solo_tabla=True)
        except Exception as e:  # ollama apagado, modelo no descargado, etc.
            mensajes.pop()
            print(f"\nError al hablar con Ollama: {e}\n"
                  f"Revisa que Ollama esté abierto y que el modelo exista:  ollama pull {modelo}\n")
            continue
        print(f"\nAsistente: {respuesta}\n" if respuesta else "")
        for tabla in tablas:
            print(tabla_texto(tabla["filas"]) + "\n")
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
