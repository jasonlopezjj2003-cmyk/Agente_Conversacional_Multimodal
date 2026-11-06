# Asistente de inventario con IA local

Chat web que responde preguntas sobre el inventario de un negocio usando un modelo de IA que corre **en tu propio equipo, sin internet**. La IA no calcula ni inventa cifras: cada dato sale de una consulta a una base de datos SQLite (abierta en solo lectura).

Ejemplos de preguntas: "¿cuántas arandelas de 55 hay?", "tractores John Deere", "¿cuál manguera tiene más saldo?", "valor total de metales", "diferencias de conteo".

## Cómo funciona

```
Navegador (web/index.html)
        ↓
API con FastAPI (API/servidor_web.py)
        ↓
Agente (Agente_Conversacional/asistente_inventario.py)  →  Ollama (modelo local)
        ↓
DataBase/inventario.db (SQLite, solo lectura)
```

## Estructura del proyecto

```
Proyecto_IA/
├── API/
│   ├── __init__.py
│   └── servidor_web.py              Servidor web y API
├── Agente_Conversacional/
│   ├── __init__.py
│   └── asistente_inventario.py      IA + consultas a la base de datos
├── DataBase/
│   ├── importar_inventario.py       Convierte el Excel en base de datos
│   ├── Data.xlsx                    <- TU Excel va aquí (no se sube a GitHub)
│   └── inventario.db                Se genera al importar (no se sube a GitHub)
├── web/
│   └── index.html                   Página de chat
├── servidor_web                     Comando corto para arrancar todo
├── requirements.txt
└── README.md
```

## Requisitos

- **Python 3.10 o superior**
- **Ollama** (https://ollama.com), para ejecutar el modelo local
- **Git Bash** en Windows (o cualquier terminal en Linux/Mac)
- Un equipo con gráfica NVIDIA de 6 GB o más (recomendado) y 16 GB de RAM

## Instalación (una sola vez, necesita internet)

**1. Clona el repositorio e instala las librerías**

```bash
git clone https://github.com/TU_USUARIO/TU_REPO.git Proyecto_IA
cd Proyecto_IA
pip install -r requirements.txt
```

**2. Descarga el modelo de IA**

```bash
ollama pull qwen3:8b
```

Puedes usar otro modelo que soporte "tools" (llamadas a funciones); se cambia con `--modelo` o en la variable `MODELO` del archivo `servidor_web`.

**3. Coloca tu Excel del inventario**

Copia tu archivo dentro de la carpeta **`DataBase/`** con el nombre **`Data.xlsx`**:

```
Proyecto_IA/DataBase/Data.xlsx
```

El Excel debe tener una fila por producto y estos encabezados (no importa si están en mayúsculas o con acentos):

| Columna | Obligatoria | Descripción |
|---|---|---|
| `CODIGO` | Sí | Código único del producto |
| `DESCRIPCION GENERAL` | Sí | Nombre y medidas |
| `Saldo` | Sí | Cantidad en el sistema |
| `Costo` | Sí | Costo por unidad de medida |
| `Unidad De Medida` | No | Si falta, se asigna "Unidad" |
| `UBICACIÓN` | No | Si falta, se marca "SIN UBICACION" |
| `FAMILIA` | No | Categoría o marca |
| `PRIMER CONTEO`, `FECHA` | No | Para las diferencias de conteo |
| `OBSERVACIONES` | No | Notas |

Las columnas calculadas (`Total`, `DIF`, `Costo Diferencia`) no se importan: la base las calcula sola.

**4. Crea la base de datos**

Desde la carpeta principal del proyecto:

```bash
python DataBase/importar_inventario.py DataBase/Data.xlsx --db DataBase/inventario.db
```

El programa imprime un reporte de calidad de datos (códigos duplicados, ítems sin unidad o sin ubicación, descripciones repetidas). Cuando actualices el Excel, vuelve a ejecutar este comando: recrea la base desde cero.

**5. Define la clave de acceso**

```bash
echo 'tu-clave-segura' > .clave
```

Este archivo nunca se sube a GitHub.

## Uso

Con Ollama abierto, desde la carpeta principal:

```bash
./servidor_web start
```

El programa muestra las direcciones de acceso, por ejemplo:

```
Desde este equipo:   http://localhost:8000
Desde otros equipos: http://192.168.1.20:8000
```

Abre esa dirección en el navegador de cualquier PC o celular **de la misma red**. El navegador pide usuario y clave: el usuario puede ser cualquiera y la clave es la de tu archivo `.clave`.

Si Windows pregunta por el firewall, permite Python en **redes privadas**.

| Comando | Qué hace |
|---|---|
| `./servidor_web start` | Abre el servidor a la red del negocio (con clave) |
| `./servidor_web local` | Solo accesible desde este equipo (sin clave) |
| `./servidor_web importar` | Regenera la base desde `DataBase/Data.xlsx` |
| `./servidor_web ayuda` | Muestra la ayuda |

Para detenerlo, pulsa `Ctrl+C`.

### Sin el script de arranque

```bash
python -m API.servidor_web --db DataBase/inventario.db --clave "tu-clave"
```

Ejecútalo siempre desde la carpeta principal y respeta las mayúsculas de `API`.

## Qué puede responder

- Buscar productos por nombre, medida o código, con su stock, costo y ubicación (tabla de hasta 30 filas)
- Buscar por marca o familia (`JNDE` = John Deere), tolerando plurales y errores de escritura; si no hay coincidencia exacta, muestra botones con familias sugeridas
- Productos con stock bajo, por ubicación y diferencias de conteo
- Totales y rankings: valor del inventario, el de mayor saldo, etc.

## Consejos para que funcione bien

- Si `ollama ps` muestra un reparto CPU/GPU en vez de `100% GPU`, el modelo no cabe entero en la gráfica y irá más lento. Cierra programas que usen la GPU o activa `OLLAMA_FLASH_ATTENTION=1` y `OLLAMA_KV_CACHE_TYPE=q8_0`.
- La IA atiende una consulta a la vez: si dos personas preguntan a la vez, la segunda espera su turno.

## Seguridad

- La base de datos se abre en **solo lectura**: la IA no puede modificar el inventario.
- Sin `--clave`, el servidor solo acepta conexiones del propio equipo.
- La clave viaja sin cifrar (HTTP) dentro de la red local. **No expongas este servidor a internet.**
- Los datos del inventario (`.xlsx`, `.db`) y la clave (`.clave`) están excluidos del repositorio.

## Problemas frecuentes

| Error | Solución |
|---|---|
| `No module named 'api'` | Usa `API` en mayúsculas y ejecuta desde la carpeta principal |
| `No existe el archivo Data.xlsx` | Colócalo en `DataBase/Data.xlsx` o indica su ruta completa |
| `Falta la clave de acceso` | Crea el archivo `.clave` (paso 5) |
| `No pude consultar el modelo` | Abre Ollama y comprueba que descargaste el modelo con `ollama pull` |
| `$'\r': command not found` | Ejecuta `sed -i 's/\r$//' servidor_web` |
| Otros equipos no pueden entrar | Permite Python en el firewall de Windows (red privada) |
