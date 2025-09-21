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


def crear_evento(nombre, telefono, fecha, hora):
    start_time = datetime.datetime.combine(fecha, hora)
    end_time = start_time + datetime.timedelta(hours=1)

    event = {
        "summary": f"Reserva de {nombre}",
        "description": f"Reserva hecha por {nombre}, teléfono: {telefono}",
        "start": {"dateTime": start_time.isoformat(), "timeZone": "Europe/Madrid"},
        "end": {"dateTime": end_time.isoformat(), "timeZone": "Europe/Madrid"},
    }

    event = service.events().insert(calendarId="primary", body=event).execute()
    return event.get("htmlLink")


def extraer_datos_reserva(historial):
    """
    Llama a OpenAI para extraer número de personas, fecha, hora y nombre del historial de conversación.
    """
    completion = client.chat.completions.create(
        model="gpt-4o-mini",
        messages=historial,
        response_format={
            "type": "json_schema",
            "json_schema": {
                "name": "reserva_schema",
                "schema": {
                    "type": "object",
                    "properties": {
                        "personas": {"type": "integer"},
                        "fecha": {"type": "string", "format": "date"},
                        "hora": {"type": "string", "format": "time"},
                        "nombre": {"type": "string"}
                    },
                    "required": ["personas", "fecha", "hora", "nombre"]
                }
            }
        }
    )

    datos = json.loads(completion.choices[0].message.content)
    return datos


@app.route("/whatsapp", methods=["POST"])
def whatsapp_reply():
    """Responder a mensajes de WhatsApp con OpenAI y memoria de conversación"""
    incoming_msg = request.form.get("Body")
    from_number = request.form.get("From")  # Ej: whatsapp:+34600123456

    resp = MessagingResponse()
    msg = resp.message()

    try:
        # Si el usuario es nuevo, inicializamos su conversación
        if from_number not in conversaciones:
            conversaciones[from_number] = [
                {"role": "system", "content": """
                Eres el asistente virtual del restaurante La Toscana.
                - Primero pide nº de personas, luego fecha/hora, luego nombre.
                - No repitas preguntas ya respondidas.
                - Cuando tengas todos los datos, confirma la reserva.
                - Usa siempre un tono breve, claro y amable, típico de WhatsApp.

                Info oficial del restaurante:
                - Dirección: Calle Mayor 123, Madrid
                - Horarios: Lunes a Viernes 13:00–23:00, Sábado y Domingo 12:00–00:00
                - Teléfono: +34 600 123 456
                - Reservas: Se pueden hacer por WhatsApp o llamando al teléfono.
                - Menú: Tenemos opciones vegetarianas y sin gluten.
                """}
            ]

        # Guardar lo que dice el usuario
        conversaciones[from_number].append({"role": "user", "content": incoming_msg})

        # Detectar si habla de reservar
        keywords_reserva = ["reservar", "reserva", "quiero reservar", "me gustaría reservar"]
        if any(kw in incoming_msg.lower() for kw in keywords_reserva):
            # Extraemos datos de la conversación con OpenAI
            datos = extraer_datos_reserva(conversaciones[from_number])

            try:
                personas = datos["personas"]
                fecha = datetime.datetime.strptime(datos["fecha"], "%Y-%m-%d").date()
                hora = datetime.datetime.strptime(datos["hora"], "%H:%M").time()
                nombre = datos["nombre"]
                telefono = from_number.replace("whatsapp:", "")

                link_evento = crear_evento(nombre, telefono, fecha, hora)

                bot_reply = (f"✅ Tu reserva para {personas} personas está confirmada.\n"
                             f"📅 Fecha: {fecha} a las {hora.strftime('%H:%M')}\n"
                             f"👤 Nombre: {nombre}\n"
                             f"🔗 Detalles: {link_evento}")

            except Exception:
                bot_reply = "⚠️ No pude registrar todos los datos. Por favor, dime nº de personas, fecha, hora y nombre."

            conversaciones[from_number].append({"role": "assistant", "content": bot_reply})
            msg.body(bot_reply)
            return str(resp)

        # 🤖 Llamar a OpenAI con todo el historial
        completion = client.chat.completions.create(
            model="gpt-4o-mini",
            messages=conversaciones[from_number]
        )

        bot_reply = completion.choices[0].message.content

        # Guardar respuesta del bot en el historial
        conversaciones[from_number].append({"role": "assistant", "content": bot_reply})

        msg.body(bot_reply)

    except Exception as e:
        msg.body(f"⚠️ Error: {str(e)}")

    return str(resp)


if __name__ == "__main__":
    from waitress import serve
    serve(app, host="0.0.0.0", port=int(os.environ.get("PORT", 5000)))
