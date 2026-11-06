#!/usr/bin/env python3
"""
Servidor web del asistente de inventario: API (FastAPI) + página de chat (web/index.html).

Funciona sin internet, dentro de la red del negocio.

Preparación (una sola vez, con internet):
    pip install fastapi uvicorn ollama

Uso:
    # Solo desde este equipo (sin clave):
    python servidor_web.py

    # Abierto a la red del negocio (exige clave):
    python servidor_web.py --clave "mi-clave-segura"
    (o define la variable de entorno INVENTARIO_CLAVE)

Luego abre en el navegador la dirección que imprime el programa,
por ejemplo http://192.168.1.20:8000 (desde cualquier celular o PC de la misma red).
El navegador pedirá usuario y clave: el usuario puede ser cualquiera; la clave es la que definiste.
"""

import argparse
import os
import re
import secrets
import socket
import sys
import threading
import time
from pathlib import Path
from typing import Optional

try:
    import ollama
    import uvicorn
    from fastapi import Depends, FastAPI, HTTPException
    from fastapi.responses import FileResponse
    from fastapi.security import HTTPBasic, HTTPBasicCredentials
    from pydantic import BaseModel
except ImportError as e:
    sys.exit(f"Falta una librería ({e.name}). Instálala con:  pip install fastapi uvicorn ollama")

try:   # estructura en carpetas: Agente_Conversacional/asistente_inventario.py
    from Agente_Conversacional.asistente_inventario import (
        INSTRUCCIONES, MAX_MENSAJES, MODELO_POR_DEFECTO, Inventario, responder)
except ImportError:   # estructura plana: todos los archivos juntos
    from asistente_inventario import (INSTRUCCIONES, MAX_MENSAJES, MODELO_POR_DEFECTO,
                                      Inventario, responder)

_AQUI = Path(__file__).resolve().parent
PAGINA = next((c for c in (_AQUI / "web" / "index.html", _AQUI.parent / "web" / "index.html") if c.exists()),
              _AQUI.parent / "web" / "index.html")
MAX_SESIONES = 100            # conversaciones abiertas a la vez
VIDA_SESION_SEG = 2 * 3600    # se descartan tras 2 horas sin uso
MAX_LARGO_MENSAJE = 1000
ID_VALIDO = re.compile(r"^[A-Za-z0-9_-]{8,64}$")


class PreguntaIn(BaseModel):
    session_id: str
    mensaje: str


def _rol(m) -> str:
    return m["role"] if isinstance(m, dict) else m.role


def limpiar_respuesta(texto: str, tablas: list) -> str:
    """Quita del texto del modelo lo que ya muestra la tabla: tablas propias y viñetas con productos listados."""
    codigos = {str(f.get("codigo")) for t in tablas for f in t["filas"] if f.get("codigo") and len(str(f["codigo"])) >= 4}
    lineas = []
    for linea in texto.split("\n"):
        if re.match(r"^\s*\|.*\|\s*$", linea):
            continue
        if re.match(r"^\s*(?:[-*•]|\d+[.)])\s+", linea) and any(c in linea for c in codigos):
            continue
        lineas.append(linea)
    return re.sub(r"\n{3,}", "\n\n", "\n".join(lineas)).strip()


def recortar_historial(mensajes: list) -> None:
    """Descarta los turnos más antiguos (siempre completos), conservando las instrucciones."""
    while len(mensajes) > MAX_MENSAJES:
        siguiente = next((i for i in range(2, len(mensajes)) if _rol(mensajes[i]) == "user"), None)
        if siguiente is None:
            break
        del mensajes[1:siguiente]


def crear_app(inv: Inventario, modelo: str, clave: Optional[str]) -> FastAPI:
    app = FastAPI(title="Asistente de inventario", docs_url=None, redoc_url=None, openapi_url=None)
    seguridad = HTTPBasic(auto_error=False)
    lock_modelo = threading.Lock()      # el modelo atiende una consulta a la vez
    lock_sesiones = threading.Lock()
    sesiones: dict = {}                 # id -> {"mensajes": [...], "ultimo": timestamp}

    def autenticar(cred: Optional[HTTPBasicCredentials] = Depends(seguridad)):
        if not clave:
            return
        if cred is None or not secrets.compare_digest(cred.password.encode(), clave.encode()):
            raise HTTPException(status_code=401, detail="Clave incorrecta",
                                headers={"WWW-Authenticate": 'Basic realm="Inventario"'})

    def obtener_sesion(sid: str) -> dict:
        ahora = time.time()
        with lock_sesiones:
            for k in [k for k, v in sesiones.items() if ahora - v["ultimo"] > VIDA_SESION_SEG]:
                del sesiones[k]
            if sid not in sesiones:
                if len(sesiones) >= MAX_SESIONES:   # descarta la más antigua
                    del sesiones[min(sesiones, key=lambda k: sesiones[k]["ultimo"])]
                sesiones[sid] = {"mensajes": [{"role": "system", "content": INSTRUCCIONES}], "ultimo": ahora}
            sesiones[sid]["ultimo"] = ahora
            return sesiones[sid]

    def validar_id(sid: str) -> str:
        if not ID_VALIDO.match(sid or ""):
            raise HTTPException(status_code=400, detail="Identificador de sesión inválido.")
        return sid

    @app.get("/")
    def pagina(_=Depends(autenticar)):
        if not PAGINA.exists():
            raise HTTPException(status_code=500, detail="Falta el archivo web/index.html")
        return FileResponse(PAGINA, media_type="text/html")

    @app.post("/api/chat")
    def chat(req: PreguntaIn, _=Depends(autenticar)):
        sid = validar_id(req.session_id)
        texto = req.mensaje.strip()
        if not texto:
            raise HTTPException(status_code=400, detail="El mensaje está vacío.")
        if len(texto) > MAX_LARGO_MENSAJE:
            raise HTTPException(status_code=400, detail=f"El mensaje supera {MAX_LARGO_MENSAJE} caracteres.")

        with lock_modelo:                       # si hay otra consulta en curso, espera su turno
            mensajes = obtener_sesion(sid)["mensajes"]
            largo_previo = len(mensajes)
            mensajes.append({"role": "user", "content": texto})
            consultas: list = []
            tablas: list = []
            try:
                respuesta = responder(ollama, inv, modelo, mensajes, verbose=False,
                                      registro=consultas, tablas=tablas, solo_tabla=True)
            except Exception as e:
                del mensajes[largo_previo:]     # deja la conversación como estaba
                print(f"[error] {type(e).__name__}: {e}", file=sys.stderr)
                raise HTTPException(
                    status_code=503,
                    detail=f"No pude consultar el modelo. Revisa que Ollama esté abierto y que '{modelo}' esté descargado.")
            recortar_historial(mensajes)
        if tablas and respuesta:
            respuesta = limpiar_respuesta(respuesta, tablas)
        return {"respuesta": respuesta, "consultas": consultas, "tablas": tablas}

    @app.post("/api/nueva")
    def nueva(req: PreguntaIn, _=Depends(autenticar)):
        sid = validar_id(req.session_id)
        with lock_sesiones:
            sesiones.pop(sid, None)
        return {"ok": True}

    @app.get("/api/salud")
    def salud(_=Depends(autenticar)):
        estado = {"base_de_datos": False, "ollama": False, "modelo": modelo, "modelo_descargado": False}
        try:
            inv.con.execute("SELECT 1 FROM productos LIMIT 1").fetchone()
            estado["base_de_datos"] = True
        except Exception:
            pass
        try:
            lista = ollama.list()
            estado["ollama"] = True
            modelos = getattr(lista, "models", None) or lista.get("models", [])
            nombres = [getattr(m, "model", None) or m.get("name", "") for m in modelos]
            estado["modelo_descargado"] = any(n == modelo or n.split(":")[0] == modelo for n in nombres)
        except Exception:
            pass
        return estado

    return app


def ip_local() -> str:
    """IP del equipo en la red local (no envía datos a internet)."""
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("10.255.255.255", 1))
        return s.getsockname()[0]
    except Exception:
        return "127.0.0.1"
    finally:
        s.close()


def main():
    ap = argparse.ArgumentParser(description="Servidor web del asistente de inventario.")
    ap.add_argument("--db", type=Path, default=Path("inventario.db"))
    ap.add_argument("--modelo", default=MODELO_POR_DEFECTO)
    ap.add_argument("--puerto", type=int, default=8000)
    ap.add_argument("--clave", default=None, help="Clave de acceso; si se define, el servidor se abre a la red")
    args = ap.parse_args()

    clave = args.clave or os.environ.get("INVENTARIO_CLAVE")
    inv = Inventario(args.db)
    app = crear_app(inv, args.modelo, clave)

    if clave:
        if len(clave) < 6:
            print("AVISO: la clave es muy corta; usa al menos 6 caracteres.")
        host = "0.0.0.0"
        print(f"\nAsistente de inventario abierto a la red del negocio (modelo: {args.modelo}).")
        print(f"  Desde este equipo:   http://localhost:{args.puerto}")
        print(f"  Desde otros equipos: http://{ip_local()}:{args.puerto}")
        print("  Si no abre desde otros equipos, permite Python en el Firewall de Windows (red privada).")
    else:
        host = "127.0.0.1"
        print(f"\nAsistente de inventario (modelo: {args.modelo}), SOLO accesible desde este equipo.")
        print(f"  Abre: http://localhost:{args.puerto}")
        print("  Para abrirlo a la red del negocio, ejecuta con --clave \"tu-clave\".")
    print("  Para detenerlo: Ctrl+C\n")
    uvicorn.run(app, host=host, port=args.puerto, log_level="warning")


if __name__ == "__main__":
    main()
