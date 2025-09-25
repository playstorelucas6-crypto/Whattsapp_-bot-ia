# main.py (versión demo personalizada Hadas Queen)
from flask import Flask, request
from twilio.twiml.messaging_response import MessagingResponse
from openai import OpenAI
import os
import datetime
import json
import dateparser
from pathlib import Path
from google.oauth2 import service_account
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

app = Flask(__name__)

# --------- CONFIG ----------
OPENAI_MODEL = "gpt-4o-mini"
TIMEZONE = "Atlantic/Canary"
CONVERS_FILE = Path("conversaciones.json")
BUSINESS_OPEN = 9   # hora inicio (9:00)
BUSINESS_CLOSE = 19 # hora cierre (19:00)
MAX_SEARCH_DAYS = 14
# --------------------------

# Inicializamos OpenAI
client = OpenAI(api_key=os.getenv("OPENAI_API_KEY"))

# Google Calendar
creds_info = json.loads(os.environ["GOOGLE_CREDENTIALS_JSON"])
creds = service_account.Credentials.from_service_account_info(
    creds_info,
    scopes=["https://www.googleapis.com/auth/calendar"]
)
service = build("calendar", "v3", credentials=creds)

# In-memory conversaciones + persistencia simple
conversaciones = {}

# ---------- Servicios Hadas Queen ----------
SERVICIOS = {
    "reductor ultra": 60,
    "piernas de acero": 60,
    "celulox brazos deluxe": 60,
    "criofrecuencia": 60,
    "ritual piel bonita": 90,
    "rejuvenecimiento facial": 75
}

SINONIMOS_SERVICIO = {
    "reductor ultra": ["reductor", "ultra", "abdomen", "flancos", "reductor ultra"],
    "piernas de acero": ["piernas de acero", "piernas", "glúteos", "acero"],
    "celulox brazos deluxe": ["celulox", "brazos", "deluxe", "celulox brazos"],
    "criofrecuencia": ["criofrecuencia", "crío", "crio"],
    "ritual piel bonita": ["piel bonita", "ritual", "exfoliación", "hidratación", "masaje hawaiano", "facial japonés"],
    "rejuvenecimiento facial": ["rejuvenecimiento", "facial", "arrugas", "líneas de expresión", "reafirmar"]
}

AFFIRMATIVE = {"sí","si","ok","vale","confirmar","claro","perfecto","sii","si claro"}

# ---------- Helpers ----------

def save_conversations():
    try:
        CONVERS_FILE.write_text(json.dumps(conversaciones, ensure_ascii=False, indent=2))
    except Exception as e:
        print("Error guardando conversaciones:", e)

def load_conversations():
    if CONVERS_FILE.exists():
        try:
            data = json.loads(CONVERS_FILE.read_text())
            return data
        except Exception as e:
            print("No se pudo cargar conversaciones:", e)
    return {}

def normalizar_servicio(servicio_text):
    if not servicio_text:
        return None
    s = servicio_text.lower()
    for key in SERVICIOS.keys():
        if key in s:
            return key
    for clave, lista in SINONIMOS_SERVICIO.items():
        for token in lista:
            if token in s:
                return clave
    return s.strip()

def parse_date_time_from_text(text):
    dt = dateparser.parse(text, languages=["es"], settings={"PREFER_DATES_FROM": "future", "RETURN_AS_TIMEZONE_AWARE": False})
    if not dt:
        return None, None
    return dt.date(), dt.time() if dt.time() != datetime.time(0,0) else None

def default_time_for_period(text):
    text = (text or "").lower()
    if "mañana" in text or "por la mañana" in text or "temprano" in text:
        return datetime.time(10,0)
    if "tarde" in text or "por la tarde" in text or "tardes" in text:
        return datetime.time(17,0)
    if "mediodía" in text or "mediodia" in text or "al mediodía" in text:
        return datetime.time(13,0)
    return None

def crear_evento(nombre, telefono, servicio, fecha, hora):
    start_time = datetime.datetime.combine(fecha, hora)
    duracion = SERVICIOS.get(servicio.lower(), 60)
    end_time = start_time + datetime.timedelta(minutes=duracion)
    event = {
        "summary": f"Cita de {nombre} - {servicio}",
        "description": f"Cita para {nombre}, teléfono: {telefono}, servicio: {servicio}",
        "start": {"dateTime": start_time.isoformat(), "timeZone": TIMEZONE},
        "end": {"dateTime": end_time.isoformat(), "timeZone": TIMEZONE},
    }
    try:
        ev = service.events().insert(
            calendarId=os.environ.get("GOOGLE_CALENDAR_ID", "primary"),
            body=event
        ).execute()
        return ev.get("htmlLink")
    except HttpError as e:
        print("Google API error al crear evento:", e)
        return None

def hay_conflicto(fecha, hora, duracion):
    start_time = datetime.datetime.combine(fecha, hora).isoformat()
    end_time = (datetime.datetime.combine(fecha, hora) + datetime.timedelta(minutes=duracion)).isoformat()
    try:
        eventos = service.events().list(
            calendarId=os.environ.get("GOOGLE_CALENDAR_ID", "primary"),
            timeMin=start_time + "Z",
            timeMax=end_time + "Z",
            singleEvents=True,
            orderBy="startTime"
        ).execute()
        return len(eventos.get("items", [])) > 0
    except HttpError as e:
        print("Google API error list:", e)
        return True

# ---------- OpenAI helpers ----------
def detectar_intencion(mensaje):
    try:
        completion = client.chat.completions.create(
            model=OPENAI_MODEL,
            messages=[
                {"role":"system", "content":"Eres un clasificador de intenciones para el asistente de Hadas Queen. Devuelve solo una palabra: reservar, cancelar, consultar, disponibilidad, saludo, modificar, otro."},
                {"role":"user", "content": f"Clasifica este mensaje en una de esas palabras: {mensaje}"}
            ],
            response_format={
                "type":"json_schema",
                "json_schema":{
                    "name":"intencion_schema",
                    "schema":{
                        "type":"object",
                        "properties":{
                            "intencion":{"type":"string","enum":["reservar","cancelar","consultar","disponibilidad","saludo","modificar","otro"]}
                        },
                        "required":["intencion"]
                    }
                }
            }
        )
        datos = json.loads(completion.choices[0].message.content)
        return datos.get("intencion","otro")
    except Exception as e:
        print("detectar_intencion error:", e)
        return "otro"

def extraer_datos_reserva(historial):
    try:
        completion = client.chat.completions.create(
            model=OPENAI_MODEL,
            messages=historial + [
                {"role":"system", "content":"Si detectas un tratamiento, usa uno de estos valores exactos: reductor ultra, piernas de acero, celulox brazos deluxe, criofrecuencia, ritual piel bonita, rejuvenecimiento facial. Devolver JSON parcial con servicio, fecha, hora, nombre."}
            ],
            response_format={
                "type":"json_schema",
                "json_schema":{
                    "name":"reserva_schema",
                    "schema":{
                        "type":"object",
                        "properties":{
                            "servicio":{"type":"string"},
                            "fecha":{"type":"string"},
                            "hora":{"type":"string"},
                            "nombre":{"type":"string"}
                        },
                        "required":[]
                    }
                }
            }
        )
        return json.loads(completion.choices[0].message.content)
    except Exception as e:
        print("extraer_datos_reserva error:", e)
        return None

# ---------- Load persisted conversations ----------
conversaciones = load_conversations()

# ---------- Endpoint ----------
@app.route("/whatsapp", methods=["POST"])
def whatsapp_reply():
    incoming_msg = (request.form.get("Body") or "").strip()
    from_number = request.form.get("From")
    resp = MessagingResponse()
    msg = resp.message()

    try:
        if not from_number:
            msg.body("⚠️ Error: no se detectó número remitente.")
            return str(resp)

        # init conversation
        if from_number not in conversaciones:
            conversaciones[from_number] = {
                "historial": [{"role":"system","content":"Eres el asistente de Hadas Queen. Paso a paso: tratamiento, fecha, hora, nombre. Pregunta sólo lo que falta."}],
                "estado":"recogiendo_datos",
                "reserva":{},
                "confirmacion_pendiente":False
            }

        # resto del flujo igual que en la versión anterior...

    except Exception as e:
        print("Error general:", e)
        msg.body(f"⚠️ Error interno: {str(e)}")
        return str(resp)

if __name__ == "__main__":
    conversaciones.update(load_conversations())
    from waitress import serve
    serve(app, host="0.0.0.0", port=int(os.environ.get("PORT", 5000)))
