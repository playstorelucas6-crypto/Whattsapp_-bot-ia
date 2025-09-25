# main.py (versión Hadas Queen demo)
from flask import Flask, request
from twilio.twiml.messaging_response import MessagingResponse
from openai import OpenAI
import os, datetime, json, dateparser, re
from pathlib import Path
from google.oauth2 import service_account
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

app = Flask(__name__)

# --------- CONFIG ----------
OPENAI_MODEL = "gpt-4o-mini"
TIMEZONE = "Atlantic/Canary"
CONVERS_FILE = Path("conversaciones.json")
BUSINESS_OPEN = 9   # hora inicio
BUSINESS_CLOSE = 19 # hora cierre
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

# In-memory
conversaciones = {}

# Servicios Hadas Queen y duración aproximada (minutos)
SERVICIOS = {
    "reductor ultra": 60,
    "piernas de acero": 75,
    "celulox brazos deluxe": 45,
    "criofrecuencia": 60,
    "ritual piel bonita": 90,
    "rejuvenecimiento facial": 60
}

# Sinónimos
SINONIMOS_SERVICIO = {
    "reductor ultra": ["reductor ultra", "reductor", "anticelulítico abdomen", "reafirmante abdomen"],
    "piernas de acero": ["piernas de acero", "piernas", "glúteos", "drenante"],
    "celulox brazos deluxe": ["celulox brazos deluxe", "brazos", "anticelulítico brazos"],
    "criofrecuencia": ["criofrecuencia", "crio", "piernas reafirmante"],
    "ritual piel bonita": ["ritual piel bonita", "exfoliación", "masaje corporal", "facial japonés"],
    "rejuvenecimiento facial": ["rejuvenecimiento facial", "reafirma", "arrugas", "facial"]
}

AFFIRMATIVE = {"sí","si","ok","vale","confirmar","claro","perfecto","sii","si claro"}

# --------- Helpers ---------

def save_conversations():
    try:
        CONVERS_FILE.write_text(json.dumps(conversaciones, ensure_ascii=False, indent=2))
    except Exception as e:
        print("Error guardando conversaciones:", e)

def load_conversations():
    if CONVERS_FILE.exists():
        try:
            return json.loads(CONVERS_FILE.read_text())
        except:
            return {}
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
    if "mediodía" in text or "mediodia" in text:
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
        print("Google API error:", e)
        return None

def hay_conflicto(fecha, hora, duracion):
    start_time = datetime.datetime.combine(fecha, hora).isoformat()
    end_time = (datetime.datetime.combine(fecha, hora)+datetime.timedelta(minutes=duracion)).isoformat()
    try:
        eventos = service.events().list(
            calendarId=os.environ.get("GOOGLE_CALENDAR_ID","primary"),
            timeMin=start_time+"Z",
            timeMax=end_time+"Z",
            singleEvents=True,
            orderBy="startTime"
        ).execute()
        return len(eventos.get("items", [])) > 0
    except:
        return True

def find_next_available(fecha, hora, duracion, days_ahead=MAX_SEARCH_DAYS):
    if fecha is None:
        fecha = datetime.date.today()
        hora = datetime.time(BUSINESS_OPEN,0)
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
                {"role":"system","content":"Eres un clasificador de intenciones para un asistente de salón. Devuelve solo: reservar, cancelar, consultar, disponibilidad, saludo, modificar, otro."},
                {"role":"user","content": f"Clasifica este mensaje: {mensaje}"}
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
    except:
        return "otro"

def extraer_datos_reserva(historial):
    try:
        completion = client.chat.completions.create(
            model=OPENAI_MODEL,
            messages=historial + [
                {"role":"system","content":"Extrae solo JSON con las claves: servicio, fecha, hora, nombre. Usa nombres exactos: reductor ultra, piernas de acero, celulox brazos deluxe, criofrecuencia, ritual piel bonita, rejuvenecimiento facial."}
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
    except:
        return None

# ---------- Load conversations ----------
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
            msg.body("⚠️ No se detectó número de remitente.")
            return str(resp)

        # init conversation
        if from_number not in conversaciones:
            conversaciones[from_number] = {
                "historial":[{"role":"system","content":"Eres el asistente de Hadas Queen. Paso a paso: servicio, fecha, hora, nombre. Pregunta solo lo que falta."}],
                "estado":"recogiendo_datos",
                "reserva":{},
                "confirmacion_pendiente":False
            }

        # append user message
        conversaciones[from_number]["historial"].append({"role":"user","content":incoming_msg})
        reservas = conversaciones[from_number]["reserva"]
        bot_reply = None

        # --- Extraer datos
        datos = extraer_datos_reserva(conversaciones[from_number]["historial"])
        if datos:
            if datos.get("servicio"):
                reservas["servicio"] = normalizar_servicio(datos["servicio"])
            if datos.get("fecha"):
                d, t = parse_date_time_from_text(datos["fecha"])
                if d: reservas["fecha"] = d.isoformat()
            if datos.get("hora"):
                dp = dateparser.parse(datos["hora"], languages=["es"])
                if dp and dp.time() != datetime.time(0,0):
                    reservas["hora"] = dp.time().strftime("%H:%M")
                else:
                    dt_def = default_time_for_period(datos["hora"])
                    if dt_def: reservas["hora"] = dt_def.strftime("%H:%M")
            if datos.get("nombre"):
                reservas["nombre"] = datos["nombre"].strip()

        # heurística de servicio
        if "servicio" not in reservas:
            text_low = incoming_msg.lower()
            for clave, tokens in SINONIMOS_SERVICIO.items():
                if any(tok in text_low for tok in tokens):
                    reservas["servicio"] = clave
                    break

        # fecha/hora
        if "fecha" not in reservas:
            d, t = parse_date_time_from_text(incoming_msg)
            if d: reservas["fecha"] = d.isoformat()
            if t: reservas["hora"] = t.strftime("%H:%M")
        if "hora" not in reservas:
            m = re.search(r"\b([01]?\d|2[0-3]):([0-5]\d)\b", incoming_msg)
            if m: reservas["hora"] = f"{int(m.group(1)):02d}:{m.group(2)}"

        conversaciones[from_number]["reserva"] = reservas
        save_conversations()

        # --- Flujo reserva
        missing = [k for k in ("servicio","fecha","hora","nombre") if k not in reservas]
        if missing:
            next_field = missing[0]
            if next_field == "servicio":
                bot_reply = "👉 ¿Qué tratamiento deseas? Opciones: " + ", ".join(SERVICIOS.keys())
            elif next_field == "fecha":
                bot_reply = "📅 Perfecto — ¿para qué día te viene bien?"
            elif next_field == "hora":
                bot_reply = "⏰ ¿A qué hora prefieres?"
            elif next_field == "nombre":
                bot_reply = "👤 ¿A nombre de quién hago la reserva?"
            conversaciones[from_number]["historial"].append({"role":"assistant","content":bot_reply})
            save_conversations()
            msg.body(bot_reply)
            return str(resp)

        # --- Confirmación
        if not conversaciones[from_number].get("confirmacion_pendiente"):
            bot_reply = (f"Perfecto 😊, confirmo:\n"
                         f"- Servicio: {reservas['servicio']}\n"
                         f"- Fecha: {reservas['fecha']}\n"
                         f"- Hora: {reservas['hora']}\n"
                         f"- Nombre: {reservas['nombre']}\n\n"
                         f"¿Deseas que la confirme en la agenda? (sí/no)")
            conversaciones[from_number]["confirmacion_pendiente"] = True
            conversaciones[from_number]["historial"].append({"role":"assistant","content":bot_reply})
            save_conversations()
            msg.body(bot_reply)
            return str(resp)

        # --- Confirmación pendiente
        if conversaciones[from_number].get("confirmacion_pendiente"):
            if incoming_msg.lower() in AFFIRMATIVE:
                try:
                    fecha = datetime.date.fromisoformat(reservas["fecha"])
                    hora = datetime.datetime.strptime(reservas["hora"], "%H:%M").time()
                except:
                    bot_reply = "⚠️ No pude entender la fecha/hora. Por favor indica de nuevo."
                    conversaciones[from_number]["historial"].append({"role":"assistant","content":bot_reply})
                    save_conversations()
                    msg.body(bot_reply)
                    return str(resp)
                dur = SERVICIOS.get(reservas["servicio"].lower(), 60)
                if hay_conflicto(fecha,hora,dur):
                    nd, nt = find_next_available(fecha,hora,dur)
                    if nd:
                        bot_reply = f"⚠️ Esa hora está ocupada. Puedo proponerte {nd.isoformat()} a las {nt.strftime('%H:%M')}. ¿Te sirve?"
                        conversaciones[from_number]["suggestion"] = {"fecha":nd.isoformat(),"hora":nt.strftime("%H:%M")}
                    else:
                        bot_reply = "⚠️ No encontré hueco disponible en los próximos días."
                else:
                    link = crear_evento(reservas["nombre"], from_number, reservas["servicio"], fecha, hora)
                    if link:
                        bot_reply = (f"✅ Tu cita ha sido confirmada.\n"
                                     f"📅 {fecha.isoformat()} a las {hora.strftime('%H:%M')}\n"
                                     f"💆 Servicio: {reservas['servicio']}\n"
                                     f"👤 Nombre: {reservas['nombre']}\n"
                                     f"🔗 {link}")
                        conversaciones[from_number]["estado"] = "reserva_confirmada"
                        conversaciones[from_number]["confirmacion_pendiente"] = False
                    else:
                        bot_reply = "❌ Error al guardar la cita en Google Calendar."
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
                    bot_reply = "Reserva cancelada. ¿Quieres empezar de nuevo?"
                conversaciones[from_number]["historial"].append({"role":"assistant","content":bot_reply})
                save_conversations()
                msg.body(bot_reply)
                return str(resp)

        # --- Intenciones generales y demo
        intencion = detectar_intencion(incoming_msg)
        if intencion == "saludo":
            bot_reply = "¡Hola! 👋 ¿Quieres reservar, consultar servicios o ver disponibilidad?"
        elif intencion == "consultar":
            # Responder duración o descripción de los tratamientos
            for servicio, desc in SERVICIOS.items():
                if servicio in incoming_msg.lower():
                    bot_reply = f"💆 {servicio.title()} dura aproximadamente {desc} minutos."
                    break
            else:
                bot_reply = "Ofrecemos los siguientes tratamientos: " + ", ".join(SERVICIOS.keys())
        elif intencion == "disponibilidad":
            d, t = parse_date_time_from_text(incoming_msg)
            if d and t:
                dur = 60
                if hay_conflicto(d,t,dur):
                    nd, nt = find_next_available(d,t,dur)
                    if nd:
                        bot_reply = f"⚠️ Ocupado. Próxima disponible: {nd.isoformat()} a las {nt.strftime('%H:%M')}"
                    else:
                        bot_reply = "⚠️ No encontré hueco."
                else:
                    bot_reply = f"✅ {d.isoformat()} a las {t.strftime('%H:%M')} está libre."
            elif d and not t:
                bot_reply = f"Has mencionado {d.isoformat()}. ¿A qué hora?"
            else:
                bot_reply = "¿Qué fecha/hora quieres comprobar?"
        elif intencion == "modificar":
            bot_reply = "¿Qué cita quieres cambiar? Indica fecha/hora actuales y lo que quieres cambiar."
        elif intencion == "otro":
            # Preguntas de demo: horarios de cierre, duración
            if "hora cierran" in incoming_msg.lower():
                bot_reply = f"🏢 Hoy cerramos a las {BUSINESS_CLOSE}:00
                elif "hora cierran" in incoming_msg.lower():
                    bot_reply = f"🏢 Hoy cerramos a las {BUSINESS_CLOSE}:00."
                else:
                    bot_reply = "Perdona, no entendí bien. ¿Quieres reservar, consultar servicios o comprobar disponibilidad?"

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
    serve(app, host="0.0.0.0", port=5000)
