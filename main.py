from flask import Flask, request
from twilio.twiml.messaging_response import MessagingResponse
from openai import OpenAI
import os

app = Flask(__name__)

# Inicializamos el cliente de OpenAI
client = OpenAI(api_key=os.getenv("OPENAI_API_KEY"))

@app.route("/whatsapp", methods=["POST"])
def whatsapp_reply():
    """Responder a mensajes de WhatsApp con OpenAI"""
    incoming_msg = request.form.get("Body")
    resp = MessagingResponse()
    msg = resp.message()

    try:
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
        msg.body(f"Error con la IA: {str(e)}")

    return str(resp)


if __name__ == "__main__":
    # En local/Codespaces funciona igual
    # En Render también (necesitas Procfile)
    from waitress import serve
    serve(app, host="0.0.0.0", port=int(os.environ.get("PORT", 5000)))
