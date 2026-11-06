import argparse
import base64
import json
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
        INSTRUCCIONES, MAX_MENSAJES, MODELO_POR_DEFECTO, Inventario, extraer_tabla, responder)
except ImportError:   # estructura plana: todos los archivos juntos
    from asistente_inventario import (INSTRUCCIONES, MAX_MENSAJES, MODELO_POR_DEFECTO,
                                      Inventario, extraer_tabla, responder)

_AQUI = Path(__file__).resolve().parent
PAGINA = next((c for c in (_AQUI / "web" / "index.html", _AQUI.parent / "web" / "index.html") if c.exists()),
              _AQUI.parent / "web" / "index.html")
MAX_SESIONES = 100            # conversaciones abiertas a la vez
VIDA_SESION_SEG = 2 * 3600    # se descartan tras 2 horas sin uso
MAX_LARGO_MENSAJE = 1000
ID_VALIDO = re.compile(r"^[A-Za-z0-9_-]{8,64}$")
MODELO_VISION_POR_DEFECTO = "qwen2.5vl:3b"
MAX_FOTO_B64 = 4_000_000      # ~3 MB; la página reduce las fotos antes de enviarlas

PROMPT_FOTO = (
    "Eres un asistente de un almacén de repuestos de maquinaria pesada y agrícola. "
    "Observa la imagen de una pieza o repuesto y responde SOLO con un JSON con estas claves: "
    '"descripcion": una frase corta en español de lo que ves; '
    '"palabras_clave": lista de 2 a 6 palabras en español, en singular, que usaría un almacenista para buscar '
    'esta pieza en un catálogo (tipo de pieza y su función, por ejemplo "cilindro", "hidraulico", "filtro", '
    '"rodamiento", "chumacera", "manguera", "arandela", "perno", "disco", "cadena"); '
    '"texto_visible": lista de textos, códigos o números de parte que se lean claramente en la pieza '
    "(lista vacía si no hay). No inventes marcas, medidas ni códigos que no se vean."
)


class PreguntaIn(BaseModel):
    session_id: str
    mensaje: str


class FotoIn(BaseModel):
    session_id: str
    imagen: str


class FamiliaIn(BaseModel):
    session_id: str
    familia: str


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


def interpretar_foto(modelo_vision: str, imagen: bytes) -> dict:
    """Pide al modelo de visión una descripción corta, palabras clave y texto legible de la pieza."""
    resp = ollama.chat(model=modelo_vision, format="json", options={"temperature": 0},
                       messages=[{"role": "user", "content": PROMPT_FOTO, "images": [imagen]}])
    contenido = resp.message.content or ""
    try:
        datos = json.loads(contenido)
    except ValueError:
        m = re.search(r"\{.*\}", contenido, re.S)
        try:
            datos = json.loads(m.group(0)) if m else {}
        except ValueError:
            datos = {}
    if not isinstance(datos, dict):
        datos = {}

    def lista(v):
        if isinstance(v, str):
            v = [v]
        return [str(x).strip()[:80] for x in v if str(x).strip()][:8] if isinstance(v, list) else []

    return {"descripcion": str(datos.get("descripcion") or "").strip()[:200],
            "palabras": lista(datos.get("palabras_clave")),
            "textos": lista(datos.get("texto_visible"))}


def crear_app(inv: Inventario, modelo: str, clave: Optional[str],
              modelo_vision: str = MODELO_VISION_POR_DEFECTO) -> FastAPI:
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
            sugerencias: list = []
            try:
                respuesta = responder(ollama, inv, modelo, mensajes, verbose=False,
                                      registro=consultas, tablas=tablas, solo_tabla=True,
                                      sugerencias=sugerencias)
            except Exception as e:
                del mensajes[largo_previo:]     # deja la conversación como estaba
                print(f"[error] {type(e).__name__}: {e}", file=sys.stderr)
                raise HTTPException(
                    status_code=503,
                    detail=f"No pude consultar el modelo. Revisa que Ollama esté abierto y que '{modelo}' esté descargado.")
            recortar_historial(mensajes)
        if tablas and respuesta:
            respuesta = limpiar_respuesta(respuesta, tablas)
        return {"respuesta": respuesta, "consultas": consultas, "tablas": tablas, "sugerencias": sugerencias}

    @app.post("/api/foto")
    def foto(req: FotoIn, _=Depends(autenticar)):
        """Búsqueda por foto: el modelo de visión describe la pieza y se buscan productos parecidos."""
        sid = validar_id(req.session_id)
        b64 = req.imagen.split(",", 1)[1] if req.imagen.startswith("data:") else req.imagen
        if len(b64) > MAX_FOTO_B64:
            raise HTTPException(status_code=413, detail="La foto es demasiado grande.")
        try:
            raw = base64.b64decode(b64, validate=True)
        except Exception:
            raise HTTPException(status_code=400, detail="No pude leer la foto.")
        if not (raw.startswith(b"\xff\xd8\xff") or raw.startswith(b"\x89PNG")):
            raise HTTPException(status_code=400, detail="Formato no compatible: usa una foto JPG o PNG.")

        with lock_modelo:
            try:
                info = interpretar_foto(modelo_vision, raw)
            except Exception as e:
                print(f"[error foto] {type(e).__name__}: {e}", file=sys.stderr)
                raise HTTPException(
                    status_code=503,
                    detail=f"No pude analizar la foto. Revisa que Ollama esté abierto y que '{modelo_vision}' "
                           f"esté descargado (ollama pull {modelo_vision}).")
            resultado = inv.buscar_por_descripcion_foto(info["palabras"], info["textos"])
            tablas, sugerencias = [], []
            tabla = extraer_tabla(resultado)
            if tabla:
                tablas.append(tabla)
            elif info["palabras"]:   # sin productos parecidos: probar por familia
                por_familia = inv.buscar_por_familia(" ".join(info["palabras"]), sugerir_todas=False)
                if por_familia:
                    tabla = extraer_tabla(por_familia)
                    if tabla:
                        tablas.append(tabla)
                    sugerencias = por_familia.get("sugerencias") or []

            partes = []
            if info["descripcion"]:
                partes.append(f"En la foto veo: {info['descripcion']}")
            if info["palabras"]:
                partes.append("Busqué por: " + ", ".join(info["palabras"]) + ".")
            if info["textos"]:
                partes.append("Texto legible en la pieza: " + ", ".join(info["textos"]) + ".")
            if not tablas and not sugerencias:
                partes.append("No encontré productos parecidos en el inventario. Prueba con otra foto o escribe el nombre de la pieza.")
            else:
                partes.append("Son posibles coincidencias: confirma la pieza, porque una foto no permite verificar medidas.")

            mensajes = obtener_sesion(sid)["mensajes"]
            mensajes.append({"role": "user", "content": "(El usuario subió una foto de una pieza.)"})
            mensajes.append({"role": "assistant", "content": "(Se analizó la foto y se mostraron posibles coincidencias.)"})
            recortar_historial(mensajes)
        return {"respuesta": "\n".join(partes), "tablas": tablas, "sugerencias": sugerencias,
                "consultas": [{"herramienta": "buscar_por_foto", "argumentos": {"palabras": ", ".join(info["palabras"])}}]}

    @app.post("/api/familia")
    def familia(req: FamiliaIn, _=Depends(autenticar)):
        """Botones de sugerencia: muestra los productos de una familia sin pasar por el modelo."""
        sid = validar_id(req.session_id)
        nombre = req.familia.strip()
        if not nombre or len(nombre) > 100:
            raise HTTPException(status_code=400, detail="Familia inválida.")
        with lock_modelo:
            resultado = inv.productos_de_familia(nombre)
            tabla = extraer_tabla(resultado)
            if not tabla:
                raise HTTPException(status_code=404, detail=resultado.get("error", "No encontré esa familia."))
            tabla["titulo"] = tabla.get("titulo") or f"Familia: {nombre}"
            mensajes = obtener_sesion(sid)["mensajes"]
            mensajes.append({"role": "user", "content": f"Muéstrame los productos de la familia {nombre}."})
            mensajes.append({"role": "assistant", "content": "(Se mostró al usuario la tabla con los resultados.)"})
            recortar_historial(mensajes)
        return {"respuesta": "", "tablas": [tabla], "sugerencias": [],
                "consultas": [{"herramienta": "productos_de_familia", "argumentos": {"familia": nombre}}]}

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
    ap.add_argument("--modelo-vision", default=MODELO_VISION_POR_DEFECTO,
                    help="Modelo de Ollama con visión, para la búsqueda por foto")
    ap.add_argument("--puerto", type=int, default=8000)
    ap.add_argument("--clave", default=None, help="Clave de acceso; si se define, el servidor se abre a la red")
    args = ap.parse_args()

    clave = args.clave or os.environ.get("INVENTARIO_CLAVE")
    inv = Inventario(args.db)
    app = crear_app(inv, args.modelo, clave, args.modelo_vision)

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
