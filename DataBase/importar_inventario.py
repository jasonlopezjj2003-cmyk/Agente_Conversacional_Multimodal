#!/usr/bin/env python3
"""
Importa el inventario desde Excel a una base de datos SQLite local.

Uso:
    python importar_inventario.py inventario.xlsx
    python importar_inventario.py inventario.xlsx --db inventario.db
    python importar_inventario.py inventario.xlsx --buscar "arandela 55"

Requisitos (una sola vez, con internet):
    pip install pandas openpyxl

Qué hace:
  1. Lee el Excel y normaliza los nombres de columnas.
  2. Limpia los datos (comillas dobles, espacios, decimales, valores vacíos).
  3. Descarta las columnas calculadas (ITEM, Total, DIF, Costo Diferencia);
     la base de datos las calcula al consultar, así nunca quedan desactualizadas.
  4. Crea las tablas `productos` y `conteos`, las vistas `v_inventario` y
     `v_diferencias`, y un índice de búsqueda de texto (`productos_fts`).
  5. Imprime un reporte con los problemas de calidad de datos encontrados.

Es seguro volver a ejecutarlo: borra y recrea la base de datos cada vez.
"""

import argparse
import re
import sqlite3
import sys
import unicodedata
from pathlib import Path

import pandas as pd

SIN_UBICACION = "SIN UBICACION"
UNIDAD_POR_DEFECTO = "Unidad"

# Nombre normalizado de la columna en el Excel -> nombre interno
COLUMNAS = {
    "codigo": "codigo",
    "descripcion general": "descripcion",
    "unidad de medida": "unidad",
    "ubicacion": "ubicacion",
    "saldo": "saldo",
    "primer conteo": "conteo",
    "fecha": "fecha_conteo",
    "costo": "costo",
    "familia": "familia",
    "observaciones": "observaciones",
}
OBLIGATORIAS = ["codigo", "descripcion", "saldo", "costo"]


# --------------------------------------------------------------------------
# Utilidades de limpieza
# --------------------------------------------------------------------------
def sin_acentos(texto: str) -> str:
    return "".join(
        c for c in unicodedata.normalize("NFD", texto) if unicodedata.category(c) != "Mn"
    )


def normalizar_encabezado(nombre) -> str:
    return re.sub(r"\s+", " ", sin_acentos(str(nombre)).strip().lower())


def limpiar_texto(valor) -> str | None:
    """Quita espacios sobrantes y arregla las comillas dobles repetidas ("" -> ")."""
    if pd.isna(valor):
        return None
    texto = re.sub(r'"{2,}', '"', str(valor))  # rachas de comillas -> una sola (pulgadas)
    texto = re.sub(r"\s+", " ", texto).strip()
    return texto or None


def convertir_fecha(serie: pd.Series) -> pd.Series:
    """Acepta fechas reales o números de serie de Excel (ej. 46113)."""
    if pd.api.types.is_datetime64_any_dtype(serie):
        return serie
    numeros = pd.to_numeric(serie, errors="coerce")
    desde_serie = pd.to_datetime(numeros, unit="D", origin="1899-12-30", errors="coerce")
    como_texto = pd.to_datetime(serie, errors="coerce")
    return desde_serie.fillna(como_texto)


# --------------------------------------------------------------------------
# Lectura y limpieza
# --------------------------------------------------------------------------
def leer_excel(ruta: Path, hoja) -> pd.DataFrame:
    df = pd.read_excel(ruta, sheet_name=hoja)
    df.columns = [normalizar_encabezado(c) for c in df.columns]

    faltan = [k for k, v in COLUMNAS.items() if v in OBLIGATORIAS and k not in df.columns]
    if faltan:
        sys.exit(
            "ERROR: faltan columnas obligatorias en el Excel: "
            + ", ".join(faltan)
            + f"\nColumnas encontradas: {list(df.columns)}"
        )

    df = df[[c for c in COLUMNAS if c in df.columns]].rename(columns=COLUMNAS)
    for opcional in ("unidad", "ubicacion", "familia", "observaciones", "conteo", "fecha_conteo"):
        if opcional not in df.columns:
            df[opcional] = None
    return df


def limpiar(df: pd.DataFrame):
    avisos = []
    n_inicial = len(df)

    # Código: texto sin espacios (evita perder ceros a la izquierda o terminar en "123.0")
    df["codigo"] = df["codigo"].apply(
        lambda v: None if pd.isna(v) else re.sub(r"\.0$", "", str(v).strip())
    )
    vacios = df["codigo"].isna() | (df["codigo"] == "")
    if vacios.any():
        avisos.append(f"{int(vacios.sum())} filas sin código fueron descartadas.")
        df = df[~vacios].copy()

    # Duplicados de código: se conserva la primera aparición
    dup = df["codigo"].duplicated(keep="first")
    if dup.any():
        lista = ", ".join(df.loc[dup, "codigo"].head(10))
        avisos.append(f"{int(dup.sum())} códigos duplicados descartados (ej.: {lista}).")
        # Si las filas repetidas tienen saldos distintos, hay que revisarlas a mano
        repetidos = df[df["codigo"].isin(df.loc[dup, "codigo"])].copy()
        repetidos["_saldo"] = pd.to_numeric(repetidos["saldo"], errors="coerce")
        distintos = repetidos.groupby("codigo")["_saldo"].nunique()
        distintos = distintos[distintos > 1].index.tolist()
        if distintos:
            avisos.append(
                "ATENCION: estos códigos duplicados tienen saldos DISTINTOS entre sus filas; "
                f"se conservó solo la primera, revisa el Excel: {', '.join(distintos)}."
            )
        df = df[~dup].copy()

    for col in ("descripcion", "unidad", "ubicacion", "familia", "observaciones"):
        df[col] = df[col].apply(limpiar_texto)

    # Valores vacíos o placeholders
    df["descripcion"] = df["descripcion"].fillna("(SIN DESCRIPCION)")
    sin_unidad = df["unidad"].isna()
    if sin_unidad.any():
        avisos.append(
            f"{int(sin_unidad.sum())} ítems sin unidad de medida "
            f"(ej.: {', '.join(df.loc[sin_unidad, 'codigo'].head(5))}); se asignó '{UNIDAD_POR_DEFECTO}'."
        )
    df["unidad"] = df["unidad"].fillna(UNIDAD_POR_DEFECTO)

    sin_ubic = df["ubicacion"].isna() | (df["ubicacion"] == "-")
    if sin_ubic.any():
        avisos.append(
            f"{int(sin_ubic.sum())} ítems sin ubicación (vacía o '-'); se marcaron '{SIN_UBICACION}'."
        )
    df.loc[sin_ubic, "ubicacion"] = SIN_UBICACION
    df["familia"] = df["familia"].fillna("SIN FAMILIA")

    # Números: quitar ruido de punto flotante (78.680000000000007 -> 78.68)
    for col in ("saldo", "costo", "conteo"):
        df[col] = pd.to_numeric(df[col], errors="coerce")
    for col, decimales in (("saldo", 4), ("conteo", 4), ("costo", 2)):
        df[col] = df[col].round(decimales)

    malos = df["saldo"].isna() | df["costo"].isna()
    if malos.any():
        avisos.append(
            f"{int(malos.sum())} filas con saldo o costo no numérico; se tomaron como 0 "
            f"(ej.: {', '.join(df.loc[malos, 'codigo'].head(5))})."
        )
        df[["saldo", "costo"]] = df[["saldo", "costo"]].fillna(0)

    negativos = df["saldo"] < 0
    if negativos.any():
        avisos.append(f"{int(negativos.sum())} ítems con saldo negativo (revisar).")

    df["fecha_conteo"] = convertir_fecha(df["fecha_conteo"])

    # Descripciones repetidas con distinto código (solo aviso)
    clave = df["descripcion"].str.upper()
    repetidas = clave.duplicated(keep=False)
    if repetidas.any():
        codigos = ", ".join(df.loc[repetidas, "codigo"].head(6))
        avisos.append(
            f"{int(repetidas.sum())} ítems comparten descripción con otro código "
            f"(lotes distintos, se conservan todos): {codigos}."
        )

    return df, avisos, n_inicial


# --------------------------------------------------------------------------
# Base de datos
# --------------------------------------------------------------------------
ESQUEMA = """
PRAGMA foreign_keys = ON;

CREATE TABLE productos (
    codigo        TEXT PRIMARY KEY,
    descripcion   TEXT NOT NULL,
    unidad        TEXT NOT NULL,
    ubicacion     TEXT NOT NULL,
    familia       TEXT NOT NULL,
    saldo         REAL NOT NULL DEFAULT 0,   -- saldo del sistema (admite decimales)
    costo         REAL NOT NULL DEFAULT 0,   -- costo por unidad de medida
    observaciones TEXT
);

CREATE TABLE conteos (
    id        INTEGER PRIMARY KEY AUTOINCREMENT,
    codigo    TEXT NOT NULL REFERENCES productos(codigo),
    fecha     TEXT,                          -- formato AAAA-MM-DD
    cantidad  REAL NOT NULL
);

CREATE INDEX idx_productos_familia   ON productos(familia);
CREATE INDEX idx_productos_ubicacion ON productos(ubicacion);
CREATE INDEX idx_conteos_codigo      ON conteos(codigo);

-- Las columnas calculadas viven en vistas: nunca se desactualizan.
CREATE VIEW v_inventario AS
SELECT p.codigo, p.descripcion, p.unidad, p.ubicacion, p.familia,
       p.saldo, p.costo,
       ROUND(p.saldo * p.costo, 2) AS total
FROM productos p;

CREATE VIEW v_diferencias AS
SELECT p.codigo, p.descripcion, p.unidad, p.ubicacion, p.familia,
       p.saldo,
       c.cantidad AS contado,
       c.fecha    AS fecha_conteo,
       ROUND(c.cantidad - p.saldo, 4)              AS diferencia,
       ROUND((c.cantidad - p.saldo) * p.costo, 2)  AS costo_diferencia
FROM productos p
JOIN conteos c ON c.codigo = p.codigo
WHERE c.id = (SELECT MAX(id) FROM conteos WHERE codigo = p.codigo)  -- conteo más reciente
  AND ROUND(c.cantidad - p.saldo, 4) <> 0;
"""


def fts5_disponible(con: sqlite3.Connection) -> bool:
    try:
        con.execute("CREATE VIRTUAL TABLE temp._t USING fts5(x)")
        con.execute("DROP TABLE temp._t")
        return True
    except sqlite3.OperationalError:
        return False


def crear_base(df: pd.DataFrame, ruta_db: Path) -> bool:
    if ruta_db.exists():
        ruta_db.unlink()
    con = sqlite3.connect(ruta_db)
    try:
        con.executescript(ESQUEMA)

        con.executemany(
            "INSERT INTO productos VALUES (?,?,?,?,?,?,?,?)",
            df[["codigo", "descripcion", "unidad", "ubicacion", "familia",
                "saldo", "costo", "observaciones"]]
            .astype(object).where(df.notna(), None).itertuples(index=False, name=None),
        )

        conteos = df.dropna(subset=["conteo"])
        filas = [
            (r.codigo, None if pd.isna(r.fecha_conteo) else r.fecha_conteo.strftime("%Y-%m-%d"), float(r.conteo))
            for r in conteos.itertuples()
        ]
        con.executemany("INSERT INTO conteos (codigo, fecha, cantidad) VALUES (?,?,?)", filas)

        # Búsqueda de texto: ignora mayúsculas y acentos
        usa_fts = fts5_disponible(con)
        if usa_fts:
            con.execute(
                "CREATE VIRTUAL TABLE productos_fts USING fts5("
                "codigo UNINDEXED, descripcion, tokenize='unicode61 remove_diacritics 2')"
            )
            con.execute("INSERT INTO productos_fts SELECT codigo, descripcion FROM productos")
        con.commit()
        return usa_fts
    finally:
        con.close()


def buscar(ruta_db: Path, texto: str, limite: int = 10):
    """Búsqueda tolerante: cada palabra debe aparecer (en cualquier orden)."""
    palabras = re.findall(r"\w+", sin_acentos(texto), flags=re.UNICODE)
    if not palabras:
        return []
    con = sqlite3.connect(ruta_db)
    con.row_factory = sqlite3.Row
    try:
        try:
            consulta = " AND ".join(f'"{p}"*' for p in palabras)
            return con.execute(
                """SELECT v.codigo, v.descripcion, v.unidad, v.ubicacion, v.saldo, v.costo, v.total
                   FROM productos_fts f JOIN v_inventario v ON v.codigo = f.codigo
                   WHERE productos_fts MATCH ? ORDER BY rank LIMIT ?""",
                (consulta, limite),
            ).fetchall()
        except sqlite3.OperationalError:  # sin FTS5: respaldo con LIKE
            condiciones = " AND ".join("descripcion LIKE ?" for _ in palabras)
            return con.execute(
                f"SELECT codigo, descripcion, unidad, ubicacion, saldo, costo, total "
                f"FROM v_inventario WHERE {condiciones} LIMIT ?",
                [f"%{p}%" for p in palabras] + [limite],
            ).fetchall()
    finally:
        con.close()


# --------------------------------------------------------------------------
# Programa principal
# --------------------------------------------------------------------------
def main():
    ap = argparse.ArgumentParser(description="Importa el inventario de Excel a SQLite.")
    ap.add_argument("excel", type=Path, help="Archivo .xlsx del inventario")
    ap.add_argument("--db", type=Path, default=Path("inventario.db"), help="Base de datos de salida")
    ap.add_argument("--hoja", default=0, help="Nombre o número de hoja (por defecto, la primera)")
    ap.add_argument("--buscar", help="Prueba una búsqueda al terminar (ej. 'arandela 55')")
    args = ap.parse_args()

    if not args.excel.exists():
        sys.exit(f"ERROR: no existe el archivo {args.excel}")

    hoja = int(args.hoja) if str(args.hoja).isdigit() else args.hoja
    df = leer_excel(args.excel, hoja)
    df, avisos, n_inicial = limpiar(df)
    usa_fts = crear_base(df, args.db)

    con = sqlite3.connect(args.db)
    n_prod = con.execute("SELECT COUNT(*) FROM productos").fetchone()[0]
    n_cont = con.execute("SELECT COUNT(*) FROM conteos").fetchone()[0]
    n_dif = con.execute("SELECT COUNT(*) FROM v_diferencias").fetchone()[0]
    valor, dif_valor = con.execute(
        "SELECT ROUND(SUM(total),2), (SELECT ROUND(SUM(costo_diferencia),2) FROM v_diferencias) "
        "FROM v_inventario"
    ).fetchone()
    con.close()

    print(f"\nBase de datos creada: {args.db.resolve()}")
    print(f"  Filas leídas del Excel : {n_inicial}")
    print(f"  Productos importados   : {n_prod}")
    print(f"  Conteos importados     : {n_cont}")
    print(f"  Ítems con diferencia   : {n_dif}  (valor: {dif_valor})")
    print(f"  Valor total inventario : {valor}")
    print(f"  Búsqueda de texto      : {'FTS5 activa' if usa_fts else 'no disponible (se usará LIKE)'}")

    if avisos:
        print("\nAvisos de calidad de datos:")
        for a in avisos:
            print(f"  - {a}")
    else:
        print("\nSin avisos de calidad de datos.")

    if args.buscar:
        print(f"\nBúsqueda de prueba: '{args.buscar}'")
        resultados = buscar(args.db, args.buscar)
        if not resultados:
            print("  (sin resultados)")
        for r in resultados:
            print(f"  {r['codigo']} | {r['descripcion']} | {r['saldo']} {r['unidad']} | "
                  f"ubic. {r['ubicacion']} | costo {r['costo']}")


if __name__ == "__main__":
    main()
