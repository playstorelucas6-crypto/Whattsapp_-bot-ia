# main.py (versión Hadas Queen para demo)
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
BUSINESS_OPEN = 9
BUSINESS_CLOSE = 19
MAX_SEARCH_DAYS = 14
# --------------------------

client = OpenAI(api_key=os.getenv("OPENAI_API_KEY"))

# Google Calendar
creds_info = json.loads(os.environ["GOOGLE_CREDENTIALS_JSON"])
creds = service_account.Credentials.from_service_account_info(
    creds_info,
    scopes=["https://www.googleapis.com/auth/calendar"]
)
service = build("calendar", "v3", credentials=creds)

conversaciones = {}

# --- Servicios Hadas Queen ---
SERVICIOS = {
    "reductor ultra": 60,
    "piernas de acero": 60,
    "celulox brazos deluxe": 60,
    "criofrecuencia": 60,
    "ritual piel bonita": 120,
    "rejuvenecimiento facial": 60
}

SINONIMOS_SERVICIO = {
    "reductor ultra": ["reductor ultra", "reductor", "anticelulítico abdomen", "reafirmante abdomen"],
    "piernas de acero": ["piernas de acero", "piernas", "glúteos", "drenante piernas"],
    "celulox brazos deluxe": ["celulox brazos deluxe", "brazos", "anticelulítico brazos"],
    "criofrecuencia": ["criofrecuencia", "piernas crio", "reafirmante piernas"],
    "ritual piel bonita": ["ritual piel bonita", "ritual", "exfoliación", "masaje hawaiano", "facial japonés"],
    "rejuvenecimiento facial": ["rejuvenecimiento facial", "facial", "arrugas", "reafirmar rostro"]
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
            return json.loads(CONVERS_FILE.read_text())
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
    if "mañana" in text or "temprano" in text:
        return datetime.time(10,0)
    if "tarde" in text:
        return datetime.time(17,0)
    if "mediodía" in text:
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

# ---------- OpenAI helpers ----------
def detectar_intencion(mensaje):
    try:
        completion = client.chat.completions.create(
            model=OPENAI_MODEL,
            messages=[
                {"role":"system", "content":"Eres un clasificador de intenciones para un asistente de salón de belleza. Devuelve solo una palabra: reservar, cancelar, consultar, disponibilidad, saludo, modificar, otro."},
                {"role":"user", "content": f"Clasifica este mensaje en una de esas palabras: {mensaje}"}
            ],
            response_format={"type":"json_schema","json_schema":{"name":"intencion_schema","schema":{"type":"object","properties":{"intencion":{"type":"string","enum":["reservar","cancelar","consultar","disponibilidad","saludo","modificar","otro"]}},"required":["intencion"]}}
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
                {"role":"system","content":f"Si detectas un servicio, usa uno de estos valores exactos: {', '.join(SERVICIOS.keys())}. Devolver solo JSON con claves que encuentres (servicio, fecha, hora, nombre)."}
            ],
            response_format={"type":"json_schema","json_schema":{"name":"reserva_schema","schema":{"type":"object","properties":{"servicio":{"type":"string"},"fecha":{"type":"string"},"hora":{"type":"string"},"nombre":{"type":"string"}},"required":[]}}}
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

        if from_number not in conversaciones:
            conversaciones[from_number] = {
                "historial": [{"role":"system","content":
                    "Eres el asistente del salón Hadas Queen. Paso a paso: servicio, fecha, hora, nombre. Pregunta sólo lo que falta."}],
                "estado":"recogiendo_datos",
                "reserva":{},
                "confirmacion_pendiente":False
            }

        conversaciones[from_number]["historial"].append({"role":"user","content":incoming_msg})
        reservas = conversaciones[from_number]["reserva"]

        # Extraer datos parciales
        datos = extraer_datos_reserva(conversaciones[from_number]["historial"])
        if datos:
            if datos.get("servicio"):
                reservas["servicio"] = normalizar_servicio(datos["servicio"])
            if datos.get("fecha"):
                d, t = parse_date_time_from_text(datos["fecha"])
                if d:
                    reservas["fecha"] = d.isoformat()
            if datos.get("hora"):
                dp = dateparser.parse(datos["hora"], languages=["es"])
                if dp and dp.time() != datetime.time(0,0):
                    reservas["hora"] = dp.time().strftime("%H:%M")
                else:
                    dt_def = default_time_for_period(datos["hora"])
                    if dt_def:
                        reservas["hora"] = dt_def.strftime("%H:%M")
            if datos.get("nombre"):
                reservas["nombre"] = datos["nombre"].strip()

        # Heurística rápida
        if "servicio" not in reservas:
            for clave, tokens in SINONIMOS_SERVICIO.items():
                if any(tok in incoming_msg.lower() for tok in tokens):
                    reservas["servicio"] = clave
                    break

        # Fechas y horas implícitas
        if "fecha" not in reservas:
            d, t = parse_date_time_from_text(incoming_msg)
            if d:
                reservas["fecha"] = d.isoformat()
                if t:
                    reservas["hora"] = t.strftime("%H:%M")
        if "hora" not in reservas:
            import re
            m = re.search(r"\b([01]?\d|2[0-3]):([0-5]\d)\b", incoming_msg)
            if m:
                reservas["hora"] = f"{int(m.group(1)):02d}:{m.group(2)}"

        conversaciones[from_number]["reserva"] = reservas
        save_conversations()

        # Preguntar solo lo que falta
        missing = [k for k in ("servicio","fecha","hora","nombre") if k not in reservas]
        if missing:
            next_field = missing[0]
            if next_field == "servicio":
                bot_reply = f"👉 ¿Qué tratamiento deseas? Opciones: {', '.join(SERVICIOS.keys())}."
            elif next_field == "fecha":
                bot_reply = "📅 Perfecto — ¿para qué día te viene bien? (ej. 3 de agosto, mañana)"
            elif next_field == "hora":
                bot_reply = "⏰ ¿A qué hora prefieres? (ej. 17:00 o 'por la tarde')"
            elif next_field == "nombre":
                bot_reply = "👤 ¿A nombre de quién hago la reserva?"
            conversaciones[from_number]["historial"].append({"role":"assistant","content":bot_reply})
            save_conversations()
            msg.body(bot_reply)
            return str(resp)

        # Confirmación
        if not conversaciones[from_number].get("confirmacion_pendiente"):
            bot_reply = (f"Perfecto 😊, confirmo:\n"
                         f"- Tratamiento: {reservas['servicio']}\n"
                         f"- Fecha: {reservas['fecha']}\n"
                         f"- Hora: {reservas['hora']}\n"
                         f"- Nombre: {reservas['nombre']}\n\n"
                         f"¿Deseas que la confirme en la agenda? (sí/no)")
            conversaciones[from_number]["confirmacion_pendiente"] = True
            conversaciones[from_number]["historial"].append({"role":"assistant","content":bot_reply})
            save_conversations()
            msg.body(bot_reply)
            return str(resp)

        # Confirmación pendiente
        if conversaciones[from_number].get("confirmacion_pendiente"):
            if incoming_msg.lower() in AFFIRMATIVE:
                try:
                    fecha = datetime.date.fromisoformat(reservas["fecha"])
                    hora = datetime.datetime.strptime(reservas["hora"], "%H:%M").time()
                except Exception:
                    bot_reply = "⚠️ No pude entender la fecha/hora. ¿Puedes escribir la fecha y la hora nuevamente?"
                    conversaciones[from_number]["historial"].append({"role":"assistant","content":bot_reply})
                    save_conversations()
                    msg.body(bot_reply)
                    return str(resp)

                dur = SERVICIOS.get(reservas["servicio"].lower(), 60)
                if hay_conflicto(fecha, hora, dur):
                    nd, nt = find_next_available(fecha, hora, dur)
                    if nd:
                        bot_reply = (f"⚠️ Esa hora está ocupada. Puedo proponerte: {nd.isoformat()} a las {nt.strftime('%H:%M')}. "
                                     f"¿Te sirve? (sí/no)")
                        conversaciones[from_number]["suggestion"] = {"fecha":nd.isoformat(), "hora":nt.strftime("%H:%M")}
                    else:
                        bot_reply = "⚠️ Lo siento, no encontré hueco disponible en los próximos días. ¿Quieres probar otra fecha?"
                else:
                    link = crear_evento(reservas["nombre"], from_number, reservas["servicio"], fecha, hora)
                    if link:
                        bot_reply = (f"✅ Tu cita ha sido confirmada.\n"
                                     f"📅 {fecha.isoformat()} a las {hora.strftime('%H:%M')}\n"
                                     f"💆 Tratamiento: {reservas['servicio']}\n"
                                     f"👤 Nombre: {reservas['nombre']}\n"
                                     f"🔗 {link}")
                        conversaciones[from_number]["estado"] = "reserva_confirmada"
                        conversaciones[from_number]["confirmacion_pendiente"] = False
                    else:
                        bot_reply = "❌ Ha ocurrido un error al guardar la cita en Google Calendar."
                conversaciones[from_number]["historial"].append({"role":"assistant","content":bot_reply})
                save_conversations()
                msg.body(bot_reply)
                return str(resp)
            else:
                low = incoming_msg.lower()
                if "cambiar" in low or "modificar" in low:
                    conversaciones[from_number]["confirmacion_pendiente"] = False
                    bot_reply = "Perfecto, dime qué quieres cambiar (servicio, fecha, hora, nombre)."
                else:
                    conversaciones[from_number]["reserva"] = {}
                    conversaciones[from_number]["confirmacion_pendiente"] = False
                    bot_reply = "Reserva cancelada. Si quieres, empezamos de nuevo. ¿Qué necesitas?"
                conversaciones[from_number]["historial"].append({"role":"assistant","content":bot_reply})
                save_conversations()
                msg.body(bot_reply)
                return str(resp)

        # Flujo general según intención
        intencion = detectar_intencion(incoming_msg)
        if intencion == "saludo":
            bot_reply = "¡Hola! 👋 ¿Quieres reservar un tratamiento de Hadas Queen, consultar servicios o comprobar disponibilidad?"
        elif intencion == "consultar":
            bot_reply = f"Ofrecemos estos tratamientos: {', '.join(SERVICIOS.keys())}."
        elif intencion == "cancelar":
            bot_reply = "Dime la fecha y hora de la cita que quieres cancelar."
        elif intencion == "modificar":
            bot_reply = "¿Qué cita quieres cambiar? Indica fecha/hora actuales y lo que deseas modificar."
        elif intencion == "disponibilidad":
            d, t = parse_date_time_from_text(incoming_msg)
            if d and t:
                dur = 60
                if hay_conflicto(d,t,dur):
                    nd, nt = find_next_available(d,t,dur)
                    if nd:
                        bot_reply = f"⚠️ Está ocupado. Puedo ofrecerte: {nd.isoformat()} a las {nt.strftime('%H:%M')}. ¿Te sirve?"
                    else:
                        bot_reply = "⚠️ No encontré alternativa en los próximos días. ¿Quieres otra fecha?"
                else:
                    bot_reply = f"✅ El {d.isoformat()} a las {t.strftime('%H:%M')} está libre. ¿Quieres reservar?"
            elif d:
                bot_reply = f"Has mencionado {d.isoformat()}, ¿a qué hora te interesa ese día?"
            else:
                bot_reply = "¿Qué fecha/hora quieres comprobar?"
        else:
            bot_reply = ("Perdona, no he entendido bien — ¿quieres reservar un tratamiento, consultar servicios o comprobar disponibilidad?")
        
        conversaciones[from_number]["historial"].append({"role":"assistant","content":bot_reply})
        save_conversations()
        msg.body(bot_reply)
        return str(resp)

    except Exception as e:
        print("Error general:", e)
        msg.body(f"⚠️ Error interno: {str(e)}")
        return str(resp)

if __name__ == "__main__":
    conversaciones.update(load_conversations())
    from waitress import serve
    serve(app, host="0.0.0.0", port=int(os.environ.get("PORT", 5000)))
