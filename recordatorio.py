"""
Recordatorio de citas por Telegram.
No entra a la pagina ni toca el captcha: solo te manda el enlace
para que tu revises desde el celular.

Credenciales: variables de entorno TELEGRAM_TOKEN y TELEGRAM_CHAT_ID
(en GitHub van como Secrets).
"""

import datetime as dt
import os
import sys

import requests

URL = "https://www.citaconsular.es/es/hosteds/widgetdefault/2d8bebcf444f3db762074e5daef723a59/#services"

TOKEN = os.getenv("TELEGRAM_TOKEN", "")
CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "")


def main():
    if not TOKEN or not CHAT_ID:
        sys.exit("Faltan los secrets TELEGRAM_TOKEN / TELEGRAM_CHAT_ID")

    ahora = dt.datetime.now(dt.timezone.utc) - dt.timedelta(hours=5)  # Cancun
    texto = (f"⏰ Hora de revisar citas ({ahora:%H:%M})\n"
             f"Abre el enlace, pasa la verificacion y revisa el calendario:\n{URL}")
    r = requests.post(f"https://api.telegram.org/bot{TOKEN}/sendMessage",
                      data={"chat_id": CHAT_ID, "text": texto,
                            "disable_web_page_preview": "true"},
                      timeout=15)
    if not r.ok:
        sys.exit(f"Telegram rechazo el mensaje: {r.text}")
    print("Aviso enviado.")


if __name__ == "__main__":
    main()
