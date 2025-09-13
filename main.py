from flask import Flask, request
from twilio.twiml.messaging_response import MessagingResponse
from openai import OpenAI
import os
import datetime
import json  # 👈 añadido para leer el JSON del env var

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


@app.route("/whatsapp", methods=["POST"])
def whatsapp_reply():
    """Responder a mensajes de WhatsApp con OpenAI"""
    incoming_msg = request.form.get("Body")
    from_number = request.form.get("From")  # Ej: whatsapp:+34600123456

    resp = MessagingResponse()
    msg = resp.message()

    try:
        # Check si el usuario quiere reservar
        keywords_reserva = ["reservar", "reserva", "quiero reservar", "me gustaría reservar"]
        if any(kw in incoming_msg.lower() for kw in keywords_reserva):
            # Crear evento con datos ficticios (puedes mejorarlo luego extrayendo fecha/hora del texto)
            fecha = datetime.date.today() + datetime.timedelta(days=1)  # Mañana por defecto
            hora = datetime.time(hour=20, minute=0)  # 20:00 por defecto
            nombre = "Cliente de WhatsApp"
            telefono = from_number.replace("whatsapp:", "")

            link_evento = crear_evento(nombre, telefono, fecha, hora)

            msg.body(f"✅ Tu reserva ha sido registrada para mañana a las 20:00.\n"
                     f"📅 Puedes verla aquí: {link_evento}")
            return str(resp)

        # Si no es una reserva, usa OpenAI como siempre
        system_prompt = """
        Eres el asistente virtual del restaurante La Toscana.
        Responde siempre como si fueras el negocio.
        Aquí tienes la información oficial:

        - Dirección: Calle Mayor 123, Madrid
        - Horarios: Lunes a Viernes 13:00–23:00, Sábado y Domingo 12:00–00:00
        - Teléfono: +34 600 123 456
        - Reservas: Se pueden hacer por WhatsApp o llamando al teléfono.
        - Menú: Tenemos opciones vegetarianas y sin gluten.

        Responde de forma clara y breve, como un asistente de WhatsApp.
        """

        completion = client.chat.completions.create(
            model="gpt-4o-mini",
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": incoming_msg}
            ]
        )

        reply = completion.choices[0].message.content
        msg.body(reply)

    except Exception as e:
        msg.body(f"⚠️ Error: {str(e)}")

    return str(resp)


if __name__ == "__main__":
    # En local/Codespaces funciona igual
    # En Render también (necesitas Procfile)
    from waitress import serve
    serve(app, host="0.0.0.0", port=int(os.environ.get("PORT", 5000)))
