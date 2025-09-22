from flask import Flask, request
from twilio.twiml.messaging_response import MessagingResponse
from openai import OpenAI
import os
import datetime
import json

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

def crear_evento(nombre, telefono, servicio, fecha, hora):
    start_time = datetime.datetime.combine(fecha, hora)
    end_time = start_time + datetime.timedelta(hours=1)

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


def extraer_datos_reserva(historial):
    """
    Llama a OpenAI para extraer servicio, fecha, hora y nombre del historial de conversación.
    Maneja errores de parseo JSON.
    """
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
                        "fecha": {"type": "string", "format": "date"},
                        "hora": {"type": "string", "format": "time"},
                        "nombre": {"type": "string"}
                    },
                    "required": ["servicio", "fecha", "hora", "nombre"]
                }
            }
        }
    )

    try:
        datos = json.loads(completion.choices[0].message.content)
        return datos
    except Exception:
        return None


@app.route("/whatsapp", methods=["POST"])
def whatsapp_reply():
    """Responder a mensajes de WhatsApp con OpenAI y memoria de conversación"""
    incoming_msg = request.form.get("Body")
    from_number = request.form.get("From")  # Ej: whatsapp:+34600123456

    resp = MessagingResponse()
    msg = resp.message()

    try:
        # Reconocer saludos simples
        saludos = ["hola", "buenos días", "buenas", "hey"]
        if incoming_msg.strip().lower() in saludos:
            bot_reply = "¡Hola! 😄 Soy tu asistente de Belleza Zen Studio. ¿Quieres reservar un servicio o necesitas información?"
            msg.body(bot_reply)
            return str(resp)

        # Si el usuario es nuevo, inicializamos su conversación
        if from_number not in conversaciones:
            conversaciones[from_number] = {
                "historial": [
                    {"role": "system", "content": """
                    Eres el asistente virtual del salón de belleza Belleza Zen Studio.
                    - Primero pregunta qué servicio desea el cliente (corte, tinte, manicura, etc.).
                    - Luego pregunta la fecha y hora de la cita.
                    - Luego confirma el nombre del cliente.
                    - No repitas preguntas ya respondidas.
                    - Cuando tengas todos los datos, confirma la cita.
                    - Usa un tono breve, amable y cercano, típico de WhatsApp.
                    """}
                ],
                "estado": "inicio"
            }

        # Guardar lo que dice el usuario
        conversaciones[from_number]["historial"].append({"role": "user", "content": incoming_msg})

        # Manejo de flujo con estados
        estado = conversaciones[from_number]["estado"]

        if estado in ["inicio", "recogiendo_datos"]:
            datos = extraer_datos_reserva(conversaciones[from_number]["historial"])
            if datos:
                try:
                    servicio = datos["servicio"]
                    fecha = datetime.datetime.strptime(datos["fecha"], "%Y-%m-%d").date()
                    hora = datetime.datetime.strptime(datos["hora"], "%H:%M").time()
                    nombre = datos["nombre"]
                    telefono = from_number.replace("whatsapp:", "")

                    link_evento = crear_evento(nombre, telefono, servicio, fecha, hora)

                    bot_reply = (f"✅ Tu cita para {servicio} está confirmada.\n"
                                 f"📅 Fecha: {fecha} a las {hora.strftime('%H:%M')}\n"
                                 f"👤 Nombre: {nombre}\n"
                                 f"🔗 Detalles: {link_evento}")

                    conversaciones[from_number]["estado"] = "reserva_confirmada"

                except Exception:
                    bot_reply = "⚠️ No pude registrar todos los datos. Por favor, dime servicio, fecha, hora y nombre."
                    conversaciones[from_number]["estado"] = "recogiendo_datos"
            else:
                bot_reply = "👉 Necesito algunos datos para la cita (servicio, fecha, hora y nombre)."
                conversaciones[from_number]["estado"] = "recogiendo_datos"

            conversaciones[from_number]["historial"].append({"role": "assistant", "content": bot_reply})
            msg.body(bot_reply)
            return str(resp)

        # 🤖 Respuesta normal de OpenAI
        completion = client.chat.completions.create(
            model="gpt-4o-mini",
            messages=conversaciones[from_number]["historial"]
        )

        bot_reply = completion.choices[0].message.content
        conversaciones[from_number]["historial"].append({"role": "assistant", "content": bot_reply})

        msg.body(bot_reply)

    except Exception as e:
        msg.body(f"⚠️ Error: {str(e)}")

    return str(resp)


if __name__ == "__main__":
    from waitress import serve
    serve(app, host="0.0.0.0", port=int(os.environ.get("PORT", 5000)))
