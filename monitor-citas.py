"""
Monitor de citas - citaconsular.es (widget Bookitit)
Modificado para resolver de forma automática CAPTCHAs de Cloudflare Turnstile.
Envía reportes continuos con capturas de pantalla integradas hacia Telegram.
"""

import datetime as dt
import logging
import os
import sys
from pathlib import Path

import requests
from playwright.sync_api import sync_playwright, TimeoutError as PWTimeout
from twocaptcha import TwoCaptcha  

# ================== CONFIGURACION ==================
URL = "https://citaconsular.es"

# Tu API Key de TwoCaptcha integrada directamente
TWOCAPTCHA_API_KEY = "77aaab32216b8819fccec3509de3eade"
SERVICIO = ""

TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN", "")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "")

TEXTOS_SIN_CITA = [
    "no hay horas disponibles", "no hay citas disponibles",
    "no available hours", "no hours available", "no hay horas",
    "sin citas", "no se han encontrado",
]
TEXTOS_BLOQUEO = [
    "captcha", "acceso denegado", "access denied", "too many requests",
    "demasiadas solicitudes", "verify you are human", "verificación de seguridad",
    "verificacion de seguridad", "security verification", "just a moment",
    "verificando", "cloudflare",
]

CAPTURAS = Path("capturas_citas")
LOG_FILE = Path("monitor_citas.log")
CAPTURAS.mkdir(exist_ok=True)
# ===================================================

solver = TwoCaptcha(TWOCAPTCHA_API_KEY)

def setup_logging():
    handlers = [logging.StreamHandler(sys.stdout)]
    logging.basicConfig(level=logging.INFO, format="[%(asctime)s] %(message)s",
                        datefmt="%Y-%m-%d %H:%M:%S", handlers=handlers)
    return logging.getLogger("monitor")

log = setup_logging()

def telegram(texto, captura=None):
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        raise ValueError("ERROR CRÍTICO: Las variables están vacías en el servidor.")

    # URL oficial de la API de Telegram corregida sin asteriscos
    base = f"https://telegram.org{TELEGRAM_TOKEN}"
    
    r = requests.post(f"{base}/sendMessage", data={"chat_id": TELEGRAM_CHAT_ID, "text": texto}, timeout=15)
    if not r.ok:
        raise Exception(f"Telegram rechazó el mensaje. Respuesta de la API: {r.text}")
        
    if captura and Path(captura).exists():
        with open(captura, "rb") as f:
            r_foto = requests.post(f"{base}/sendPhoto", data={"chat_id": TELEGRAM_CHAT_ID}, files={"photo": f}, timeout=30)
            if not r_foto.ok:
                log.error(f"Telegram rechazó la foto: {r_foto.text}")

def contiene(texto, lista):
    return any(t in texto for t in lista)

def revisar(page):
    try:
        page.goto(URL, wait_until="domcontentloaded", timeout=60000)
    except Exception as e:
        return "error", f"Error al cargar: {str(e)[:120]}"

    page.wait_for_timeout(3000)
    texto_actual = page.inner_text("body").lower()

    if contiene(texto_actual, TEXTOS_BLOQUEO):
        log.info("Captcha detectado. Solicitando resolución a TwoCaptcha...")
        try:
            result = solver.turnstile(
                sitekey='0x1AAAAAAAAkg0s2VIOD34y5',
                url='https://citaconsular.es',
                data='foo',
                pagedata='bar',
                action='challenge',
                useragent=page.evaluate("navigator.userAgent")
            )
            token = result['code']
            log.info("Token de respuesta recibido con éxito.")
            
            page.evaluate(f"""
                () => {{
                    const inputs = document.querySelectorAll('textarea[name*="response"], input[name*="response"]');
                    if (inputs.length > 0) {{
                        inputs.forEach(el => el.value = "{token}");
                    }} else {{
                        const t1 = document.createElement('textarea');
                        t1.name = 'cf-turnstile-response';
                        t1.value = '{token}';
                        document.forms.appendChild(t1);
                    }}
                }}
            """)
            page.wait_for_timeout(2000)
            
        except Exception as e:
            return "bloqueo", f"Error de TwoCaptcha: {str(e)[:100]}"

    for sel in ["#idCaptchaButton", "text=Continuar", "button:has-text('Continuar')"]:
        try:
            btn = page.locator(sel).first
            if btn.is_visible(timeout=5000):
                btn.click()
                page.wait_for_timeout(5000)
                break
        except Exception:
            pass

    page.wait_for_timeout(3000)
    texto = page.inner_text("body").lower()

    if len(texto.strip()) < 40:
        return "error", "Pagina casi vacia"
    if contiene(texto, TEXTOS_BLOQUEO):
        return "bloqueo", "Sigue bloqueado por CAPTCHA"
    if contiene(texto, TEXTOS_SIN_CITA):
        return "sin_cita", "No hay horas disponibles"
    
    return "hay_cita", "¡Posible disponibilidad! El mensaje de 'no hay citas' desapareció"

def main():
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        ctx = browser.new_context(locale="es-ES", viewport={"width": 1366, "height": 900})
        page = ctx.new_page()
        
        try:
            estado, detalle = revisar(page)
        except Exception as e:
            estado, detalle = "error", str(e)[:150]
            
        captura = CAPTURAS / f"{dt.datetime.now():%Y%m%d_%H%M%S}_{estado}.png"
        try:
            page.screenshot(path=str(captura), full_page=True)
        except Exception:
            captura = None
            
        log.info(f"Resultado final: {estado} - {detalle}")
        
        if estado == "hay_cita":
            telegram(f"🚨 ¡CITAS DISPONIBLES!\nEstado: {detalle}", captura)
        elif estado == "sin_cita":
            telegram(f"🔍 Revisión automática: El monitor sigue activo. {detalle}.", captura)
        else:
            telegram(f"⚠️ Alerta en revisión del monitor: {detalle}.", captura)
            
        browser.close()

if __name__ == "__main__":
    main()
