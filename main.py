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
        completion = client.chat.completions.create(
            model="gpt-3.5-turbo",
            messages=[
                {"role": "system", "content": "Eres un asistente útil en WhatsApp."},
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
