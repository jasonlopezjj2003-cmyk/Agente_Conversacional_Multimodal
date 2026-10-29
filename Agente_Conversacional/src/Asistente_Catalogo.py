import os
import sys
import time
import threading
from dotenv import load_dotenv
import pandas as pd
from google import genai

load_dotenv()

client = genai.Client()

dir_actual = os.path.dirname(os.path.abspath(__file__))


DATA_Catalogo=os.path.join(dir_actual,"..", "Data", "Data.xlsx")


try:
    df = pd.read_excel(DATA_Catalogo, sheet_name="DATA", header=7, engine="openpyxl")
    df.columns = df.columns.astype(str).str.strip()
    
    df = df.dropna(how="all")
    print(f"Archivo {DATA_Catalogo} cargado correctamente. {len(df)} filas y {len(df.columns)} columnas.")
except Exception as e:
    print(f"Error al cargar el archivo {DATA_Catalogo}: {e}")
    exit()

print("\n --- Asistente de Catalogo --- \n")
print("Escribe tu consulta o escribe 'salir' para terminar la conversación.\n")

def indicador_carga(detener_evento):
    animacion = ["⠋", "⠙", "⠹", "⠸", "⠼", "⠴", "⠦", "⠧", "⠇", "⠏"]
    i=0
    while not detener_evento.is_set():
        sys.stdout.write(f"\rGenerando respuesta... {animacion[i % len(animacion)]}")
        sys.stdout.flush()
        time.sleep(0.1)
        i += 1
        sys.stdout.write("\r" + " " * 30 + "\r")  # Limpiar la línea después de detenerse


while True:
    pregunta= input("Pregunta: ")
    if pregunta.lower() in ["salir", "exit"]:
        print("Saliendo del asistente de catalogo. ¡Hasta luego!")
        break

    if not pregunta:
        continue

    df_filtrado = df.copy()

    palabras = pregunta.split()

    resultados_relevantes = pd.DataFrame()

    for palabra in palabras:
        if palabra.isdigit() and len(palabra) > 3:
            # Buscar por código exacto o parcial
            coincidencia = df[df.astype(str).apply(lambda x: x.str.contains(palabra, na=False, regex=False)).any(axis=1)]
            if not coincidencia.empty:
                resultados_relevantes = pd.concat([resultados_relevantes, coincidencia]).drop_duplicates()

    # Si no encontró por número exacto, buscamos por texto en la descripción
    if resultados_relevantes.empty:
        for palabra in palabras:
            if len(palabra) > 2 and palabra.lower() not in ["que", "cual", "dame", "lista", "articulo", "articulos", "por", "con"]:
                coincidencia = df[df.astype(str).apply(lambda x: x.str.contains(palabra, case=False, na=False, regex=False)).any(axis=1)]
                resultados_relevantes = pd.concat([resultados_relevantes, coincidencia]).drop_duplicates()

    
    if resultados_relevantes.empty:
        datos_a_enviar = df.head(30).to_string(index=False)
    else:
        datos_a_enviar = resultados_relevantes.head(15).to_string(index=False)

    prompt_sistema = (
        "Eres un asistente experto en gestión de almacenes e inventarios. "
        "A continuación tienes un fragmento relevante del inventario basado en la búsqueda del usuario. "
        "Responde de manera precisa, clara y concisa a la pregunta del usuario. "
        "Detalla los campos clave (Código, Descripción General, Unidad de Medida, Ubicación, Saldo, Costos).\n\n"
        f"DATOS DEL INVENTARIO:\n{datos_a_enviar}\n\n"
        f"PREGUNTA DEL USUARIO: {pregunta}"

    )

    detener_hilo= threading.Event()
    hilo = threading.Thread(target=indicador_carga, args=(detener_hilo,))

    try:
        hilo.start()
        response = client.models.generate_content(
            model="gemini-flash-latest",
            contents=prompt_sistema,
        )

        detener_hilo.set()
        hilo.join()

        print(f"\nRespuesta:\n{response.text}\n")
        print("-" * 50)

    except Exception as e:
        detener_hilo.set()
        hilo.join()
        print(f"Error al generar la respuesta: {e}")



