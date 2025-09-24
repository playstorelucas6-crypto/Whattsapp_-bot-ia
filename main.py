from flask import Flask, request
from twilio.twiml.messaging_response import MessagingResponse
from openai import OpenAI
import os
import datetime
import json
import dateparser  # ✅ Para procesar fechas naturales en español

# Google Calendar
from google.oauth2 import service_account
from googleapiclient.discovery import build

app = Flask(__name__)

# Inicializamos el cliente de OpenAI
client = OpenAI(api_key=os.getenv("OPENAI_API_KEY"))

# ✅ Cargar credenciales de Google Calendar desde variable de entorno
creds_info = json.loads(os.environ["GOOGLE_CREDENTIALS_JSON"])
creds = service_account.Credentials.from_service_account_info(
    creds_info,
    scopes=["https://www.googleapis.com/auth/calendar"]
)

service = build("calendar", "v3", credentials=creds)

# 🗂 Diccionario para guardar las conversaciones por número
conversaciones = {}

# Zona horaria para Canarias
TIMEZONE = "Atlantic/Canary"

# Servicios disponibles y duración en minutos
SERVICIOS = {
    "corte": 30,
    "tinte": 120,
    "manicura": 45,
    "pedicura": 60,
    "tratamiento facial": 90
}

def normalizar_fecha(texto):
    """Convierte expresiones como 'jueves', 'mañana' en fecha ISO"""
    dt = dateparser.parse(
        texto,
        languages=["es"],
        settings={"PREFER_DATES_FROM": "future"}
    )
    return dt.date() if dt else None

def normalizar_hora(texto):
    """Convierte expresiones como '10am', 'por la mañana' en hora"""
    dt = dateparser.parse(
        texto,
        languages=["es"],
        settings={"PREFER_DATES_FROM": "future"}
    )
    return dt.time() if dt else None

def crear_evento(nombre, telefono, servicio, fecha, hora):
    start_time = datetime.datetime.combine(fecha, hora)
    duracion = SERVICIOS.get(servicio.lower(), 60)  # default 1h
    end_time = start_time + datetime.timedelta(minutes=duracion)

    event = {
        "summary": f"Cita de {nombre} - {servicio}",
        "description": f"Cita para {nombre}, teléfono: {telefono}, servicio: {servicio}",
        "start": {"dateTime": start_time.isoformat(), "timeZone": TIMEZONE},
        "end": {"dateTime": end_time.isoformat(), "timeZone": TIMEZONE},
    }

    event = service.events().insert(
        calendarId=os.environ.get("GOOGLE_CALENDAR_ID", "primary"),
        body=event
    ).execute()
    return event.get("htmlLink")

def hay_conflicto(fecha, hora, duracion):
    """Verifica si ya existe un evento en el horario solicitado"""
    start_time = datetime.datetime.combine(fecha, hora).isoformat()
    end_time = (datetime.datetime.combine(fecha, hora) + datetime.timedelta(minutes=duracion)).isoformat()

    eventos = service.events().list(
        calendarId=os.environ.get("GOOGLE_CALENDAR_ID", "primary"),
        timeMin=start_time + "Z",
        timeMax=end_time + "Z",
        singleEvents=True,
        orderBy="startTime"
    ).execute()

    return len(eventos.get("items", [])) > 0

def extraer_datos_reserva(historial):
    completion = client.chat.completions.create(
        model="gpt-4o-mini",
        messages=historial,
        response_format={
            "type": "json_schema",
            "json_schema": {
                "name": "reserva_salon_schema",
                "schema": {
                    "type": "object",
                    "properties": {
                        "servicio": {"type": "string"},
                        "fecha": {"type": "string"},
                        "hora": {"type": "string"},
                        "nombre": {"type": "string"}
                    },
                    "required": []
                }
            }
        }
    )
    try:
        return json.loads(completion.choices[0].message.content)
    except Exception:
        return None

def detectar_intencion(mensaje):
    """Clasifica intención del mensaje"""
    completion = client.chat.completions.create(
        model="gpt-4o-mini",
        messages=[
            {"role": "system", "content": "Eres un clasificador de intenciones para un asistente de reservas en un salón de belleza. Devuelve solo una palabra."},
            {"role": "user", "content": f"Clasifica este mensaje: {mensaje}"}
        ],
        response_format={
            "type": "json_schema",
            "json_schema": {
                "name": "intencion_schema",
                "schema": {
                    "type": "object",
                    "properties": {
                        "intencion": {
                            "type": "string",
                            "enum": ["reservar", "cancelar", "consultar", "disponibilidad", "saludo", "otro"]
                        }
                    },
                    "required": ["intencion"]
                }
            }
        }
    )
    try:
        datos = json.loads(completion.choices[0].message.content)
        return datos["intencion"]
    except:
        return "otro"

@app.route("/whatsapp", methods=["POST"])
def whatsapp_reply():
    incoming_msg = request.form.get("Body")
    from_number = request.form.get("From")

    resp = MessagingResponse()
    msg = resp.message()

    try:
        # Detectar intención
        intencion = detectar_intencion(incoming_msg)

        if intencion == "saludo":
            msg.body("¡Hola! 👋 Soy tu asistente de Belleza Zen Studio. ¿Quieres reservar, cancelar, consultar o ver disponibilidad?")
            return str(resp)

        if intencion == "consultar":
            msg.body("Ofrecemos estos servicios: 💇 corte (30min), 🎨 tinte (2h), 💅 manicura (45min), 🦶 pedicura (1h), ✨ tratamiento facial (1h30).")
            return str(resp)

        if intencion == "cancelar":
            msg.body("Entendido 🙏. Dime la fecha y hora de la cita que quieres cancelar.")
            return str(resp)

        if intencion == "disponibilidad":
            fecha = normalizar_fecha(incoming_msg)
            hora = normalizar_hora(incoming_msg)
            if fecha and hora:
                duracion = 60
                if hay_conflicto(fecha, hora, duracion):
                    bot_reply = f"⚠️ El {fecha} a las {hora.strftime('%H:%M')} ya está ocupado. ¿Quieres que te sugiera otra hora?"
                else:
                    bot_reply = f"✅ El {fecha} a las {hora.strftime('%H:%M')} está libre. ¿Quieres reservar?"
            else:
                bot_reply = "📅 Dime la fecha y hora exacta que te interesa para revisar disponibilidad."
            msg.body(bot_reply)
            return str(resp)

        # --- Flujo de reservas ---
        if from_number not in conversaciones:
            conversaciones[from_number] = {
                "historial": [
                    {"role": "system", "content": """
                    Eres el asistente virtual del salón Belleza Zen Studio.
                    Guía al cliente paso a paso:
                    1. Pregunta qué servicio desea (muestra opciones si pide ayuda).
                    2. Pregunta fecha y hora.
                    3. Pregunta su nombre.
                    - Usa frases cortas y amables.
                    - Si detectas que falta algo, pregunta solo eso.
                    - Antes de confirmar, reconfirma todos los datos.
                    - Si ya tiene una cita confirmada, ofrécele ver, cancelar o cambiar.
                    """}
                ],
                "estado": "recogiendo_datos",
                "reserva": {},
                "confirmacion_pendiente": False
            }

        conversaciones[from_number]["historial"].append({"role": "user", "content": incoming_msg})
        datos = extraer_datos_reserva(conversaciones[from_number]["historial"])
        reservas = conversaciones[from_number]["reserva"]

        # Actualizar datos con normalización de fechas/horas
        if datos:
            if datos.get("servicio"): reservas["servicio"] = datos["servicio"]
            if datos.get("fecha"):
                fecha_norm = normalizar_fecha(datos["fecha"])
                if fecha_norm: reservas["fecha"] = fecha_norm.isoformat()
            if datos.get("hora"):
                hora_norm = normalizar_hora(datos["hora"])
                if hora_norm: reservas["hora"] = hora_norm.strftime("%H:%M")
            if datos.get("nombre"): reservas["nombre"] = datos["nombre"]

        # Flujo de conversación
        if "servicio" not in reservas:
            bot_reply = "👉 ¿Qué servicio deseas? Opciones: corte, tinte, manicura, pedicura, tratamiento facial."
        elif "fecha" not in reservas:
            bot_reply = "📅 Genial, ¿para qué día quieres tu cita?"
        elif "hora" not in reservas:
            bot_reply = "⏰ ¿A qué hora te viene mejor?"
        elif "nombre" not in reservas:
            bot_reply = "👤 ¿A nombre de quién hago la reserva?"
        elif not conversaciones[from_number]["confirmacion_pendiente"]:
            bot_reply = (f"Perfecto 😊, entonces sería:\n"
                         f"- Servicio: {reservas['servicio']}\n"
                         f"- Fecha: {reservas['fecha']}\n"
                         f"- Hora: {reservas['hora']}\n"
                         f"- Nombre: {reservas['nombre']}\n\n"
                         f"¿Quieres que lo confirme en la agenda? (sí/no)")
            conversaciones[from_number]["confirmacion_pendiente"] = True
        else:
            if incoming_msg.strip().lower() in ["sí", "si", "ok", "vale", "confirmar"]:
                fecha = datetime.datetime.fromisoformat(reservas["fecha"]).date()
                hora = datetime.datetime.strptime(reservas["hora"], "%H:%M").time()
                duracion = SERVICIOS.get(reservas["servicio"].lower(), 60)

                if hay_conflicto(fecha, hora, duracion):
                    bot_reply = "⚠️ Esa hora ya está ocupada. ¿Quieres que te sugiera la más cercana disponible?"
                else:
                    link_evento = crear_evento(reservas["nombre"], from_number, reservas["servicio"], fecha, hora)
                    bot_reply = (f"✅ Tu cita ha sido confirmada.\n"
                                 f"📅 {fecha} a las {hora.strftime('%H:%M')}\n"
                                 f"💇 Servicio: {reservas['servicio']}\n"
                                 f"👤 Nombre: {reservas['nombre']}\n"
                                 f"🔗 Detalles: {link_evento}")
                    conversaciones[from_number]["estado"] = "reserva_confirmada"
            else:
                bot_reply = "❌ Reserva cancelada. Si quieres empezamos de nuevo con otro servicio."

        conversaciones[from_number]["historial"].append({"role": "assistant", "content": bot_reply})
        msg.body(bot_reply)
        return str(resp)

    except Exception as e:
        msg.body(f"⚠️ Error: {str(e)}")
        return str(resp)

if __name__ == "__main__":
    from waitress import serve
    serve(app, host="0.0.0.0", port=int(os.environ.get("PORT", 5000)))
