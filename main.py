# main.py - Hadas Queen Demo

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
import re
from waitress import serve

app = Flask(__name__)

# -------------- CONFIG ----------------
OPENAI_MODEL = "gpt-4o-mini"
TIMEZONE = "Atlantic/Canary"
CONVERS_FILE = Path("conversaciones.json")
BUSINESS_OPEN = 9
BUSINESS_CLOSE = 19
MAX_SEARCH_DAYS = 14
# --------------------------------------

# Inicializamos OpenAI
client = OpenAI(api_key=os.getenv("OPENAI_API_KEY"))

# Google Calendar
creds_info = json.loads(os.environ["GOOGLE_CREDENTIALS_JSON"])
creds = service_account.Credentials.from_service_account_info(
    creds_info,
    scopes=["https://www.googleapis.com/auth/calendar"]
)
service = build("calendar", "v3", credentials=creds)

# In-memory conversaciones
conversaciones = {}

# Tratamientos Hadas Queen
SERVICIOS = {
    "reductor ultra": 60,
    "piernas de acero": 60,
    "celulox brazos deluxe": 60,
    "criofrecuencia": 60,
    "ritual piel bonita": 90,
    "rejuvenecimiento facial": 90
}

DESCRIPCIONES_SERVICIOS = {
    "reductor ultra": "Reductor, anticelulítico y reafirmante de abdomen y flancos laterales.",
    "piernas de acero": "Reductor, anticelulítico, drenante y reafirmante de piernas y glúteos.",
    "celulox brazos deluxe": "Anticelulítico, reductor y reafirmante de brazos.",
    "criofrecuencia": "Anticelulítico y reafirmante de piernas.",
    "ritual piel bonita": "Exfoliación, hidratación profunda, masaje hawaiano corporal y facial japonés.",
    "rejuvenecimiento facial": "Elimina líneas de expresión, difumina arrugas, reafirma y rejuvenece."
}

# Sinónimos para detectar servicios
SINONIMOS_SERVICIO = {k: [k] + k.split() for k in SERVICIOS.keys()}

AFFIRMATIVE = {"sí","si","ok","vale","confirmar","claro","perfecto","sii","si claro"}

# ---------------- HELPERS ----------------
def save_conversations():
    try:
        CONVERS_FILE.write_text(json.dumps(conversaciones, ensure_ascii=False, indent=2))
    except Exception as e:
        print("Error guardando conversaciones:", e)

def load_conversations():
    if CONVERS_FILE.exists():
        try:
            return json.loads(CONVERS_FILE.read_text())
        except Exception as e:
            print("No se pudo cargar conversaciones:", e)
    return {}

def normalizar_servicio(text):
    if not text:
        return None
    t = text.lower()
    for key, tokens in SINONIMOS_SERVICIO.items():
        if any(tok in t for tok in tokens):
            return key
    return t.strip()

def parse_date_time(text):
    dt = dateparser.parse(text, languages=["es"], settings={"PREFER_DATES_FROM": "future"})
    if not dt:
        return None, None
    return dt.date(), dt.time() if dt.time() != datetime.time(0,0) else None

def default_time(text):
    t = (text or "").lower()
    if "mañana" in t: return datetime.time(10,0)
    if "tarde" in t: return datetime.time(17,0)
    if "mediodía" in t or "mediodia" in t: return datetime.time(13,0)
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
        print("Google API error:", e)
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

def find_next_available(fecha, hora, duracion, days_ahead=MAX_SEARCH_DAYS):
    if fecha is None:
        fecha = datetime.date.today()
        hora = datetime.time(BUSINESS_OPEN, 0)
    start_dt = datetime.datetime.combine(fecha, hora)
    step = datetime.timedelta(minutes=30)
    limit = start_dt + datetime.timedelta(days=days_ahead)
    current = start_dt
    while current <= limit:
        if BUSINESS_OPEN <= current.hour < BUSINESS_CLOSE and current.weekday() != 6:
            if not hay_conflicto(current.date(), current.time(), duracion):
                return current.date(), current.time()
        current += step
    return None, None

# OpenAI extraction de datos
def extraer_datos_reserva(historial):
    try:
        completion = client.chat.completions.create(
            model=OPENAI_MODEL,
            messages=historial + [
                {"role":"system","content":
                 "Extrae solo JSON con servicio, fecha, hora, nombre si están presentes."}
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
        return {}

# ---------------- ENDPOINT ----------------
@app.route("/whatsapp", methods=["POST"])
def whatsapp_reply():
    incoming = (request.form.get("Body") or "").strip()
    from_number = request.form.get("From")
    resp = MessagingResponse()
    msg = resp.message()

    if not from_number:
        msg.body("⚠️ No se detectó número.")
        return str(resp)

    if from_number not in conversaciones:
        conversaciones[from_number] = {
            "historial":[{"role":"system","content":"Eres asistente Hadas Queen. Pregunta solo lo que falta: servicio, fecha, hora, nombre."}],
            "reserva":{},
            "confirmacion_pendiente":False
        }

    conv = conversaciones[from_number]
    conv["historial"].append({"role":"user","content":incoming})
    reservas = conv["reserva"]
    bot_reply = None

    # HEURÍSTICAS PRIMARIAS PARA DEMO
    text_low = incoming.lower()

    # Saludo
    if any(g in text_low for g in ["hola","buenos días","buenas tardes"]):
        bot_reply = "¡Hola! 👋 ¿Quieres reservar un tratamiento o consultar información de Hadas Queen?"
    
    # Preguntar intención de reserva
    elif "reservar" in text_low or "quiero" in text_low:
        bot_reply = "Perfecto 😊. ¿Qué tratamiento deseas? Opciones:\n" + "\n".join(f"- {k}" for k in SERVICIOS.keys())
    
    # Consultar disponibilidad
    elif "tienen hueco" in text_low or "disponible" in text_low:
        d, t = parse_date_time(incoming)
        if d:
            bot_reply = f"✅ {d.isoformat()} está libre. ¿A qué hora te interesa?"
        else:
            bot_reply = "¿Para qué fecha quieres comprobar disponibilidad?"
    
    # Consultar duración
    elif "cuanto dura" in text_low or "duración" in text_low:
        for s in SERVICIOS:
            if s in text_low:
                bot_reply = f"Una sesión de {s} dura aproximadamente {SERVICIOS[s]} minutos."
                break
    
    # Horarios de cierre
    elif "hora cierran" in text_low or "horario" in text_low:
        bot_reply = f"⏰ Hoy cerramos a las {BUSINESS_CLOSE}:00."

    # Modificar cita
    elif "cambiar" in text_low or "modificar" in text_low:
        bot_reply = "Perfecto, dime qué quieres cambiar (servicio, fecha, hora o nombre)."

    # Si estamos en flujo de reserva, extraer datos parciales
    else:
        datos = extraer_datos_reserva(conv["historial"])
        if datos.get("servicio"):
            reservas["servicio"] = normalizar_servicio(datos["servicio"])
        if datos.get("fecha"):
            d, t = parse_date_time(datos["fecha"])
            if d:
                reservas["fecha"] = d.isoformat()
            if t:
                reservas["hora"] = t.strftime("%H:%M")
        if datos.get("hora"):
            t = dateparser.parse(datos["hora"])
            if t:
                reservas["hora"] = t.strftime("%H:%M")
        if datos.get("nombre"):
            reservas["nombre"] = datos["nombre"].strip()

        # Preguntar lo que falta
        missing = [k for k in ["servicio","fecha","hora","nombre"] if k not in reservas]
        if missing:
            next_field = missing[0]
            if next_field == "servicio":
                bot_reply = "👉 ¿Qué tratamiento deseas? Opciones:\n" + "\n".join(f"- {k}" for k in SERVICIOS.keys())
            elif next_field == "fecha":
                bot_reply = "📅 ¿Para qué día te viene bien? (ej. 3 de agosto, mañana)"
            elif next_field == "hora":
                bot_reply = "⏰ ¿A qué hora prefieres? (ej. 17:00 o 'por la tarde')"
            elif next_field == "nombre":
                bot_reply = "👤 ¿A nombre de quién hago la reserva?"
        else:
            if not conv.get("confirmacion_pendiente"):
                bot_reply = (f"Perfecto 😊, confirmo tu cita:\n"
                             f"- Servicio: {reservas['servicio']}\n"
                             f"- Fecha: {reservas['fecha']}\n"
                             f"- Hora: {reservas['hora']}\n"
                             f"- Nombre: {reservas['nombre']}\n\n"
                             f"¿Deseas que la confirme? (sí/no)")
                conv["confirmacion_pendiente"] = True
            else:
                if text_low in AFFIRMATIVE:
                    fecha = datetime.date.fromisoformat(reservas["fecha"])
                    hora = datetime.datetime.strptime(reservas["hora"], "%H:%M").time()
                    link = crear_evento(reservas["nombre"], from_number, reservas["servicio"], fecha, hora)
                    if link:
                        bot_reply = (f"✅ Tu cita ha sido confirmada.\n"
                                     f"📅 {fecha.isoformat()} a las {hora.strftime('%H:%M')}\n"
                                     f"💆 {reservas['servicio']}\n"
                                     f"👤 {reservas['nombre']}\n"
                                     f"🔗 {link}")
                        conv["confirmacion_pendiente"] = False
                    else:
                        bot_reply = "❌ Error al guardar la cita."
                else:
                    bot_reply = "Reserva cancelada. Si quieres, empezamos de nuevo."
                    conv["reserva"] = {}
                    conv["confirmacion_pendiente"] = False

    conv["historial"].append({"role":"assistant","content":bot_reply})
    save_conversations()
    msg.body(bot_reply)
    return str(resp)

if __name__ == "__main__":
    conversaciones.update(load_conversations())
    serve(app, host="0.0.0.0", port=5000)
