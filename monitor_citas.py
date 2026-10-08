"""
Monitor de citas - citaconsular.es (widget Bookitit)
Modificado para resolver de forma automática CAPTCHAs de Cloudflare Turnstile.

Instalacion:
    py -m pip install playwright requests plyer twocaptcha
    py -m playwright install chromium
"""

import argparse
import datetime as dt
import logging
import os
import random
import sys
import time
from pathlib import Path

import requests
from playwright.sync_api import sync_playwright, TimeoutError as PWTimeout
from twocaptcha import TwoCaptcha  

# ================== CONFIGURACION ==================
URL = "https://citaconsular.es"

# Tu API Key de TwoCaptcha integrada directamente
TWOCAPTCHA_API_KEY = "77aaab32216b8819fccec3509de3eade"

# Texto del servicio a seleccionar, p.ej. "Pasaporte". Vacio = no selecciona.
SERVICIO = ""

MIN_ESPERA = 35
MAX_ESPERA = 70
COOLDOWN_BLOQUEO = 120
MAX_REVISIONES_DIA = 28
HORA_HEARTBEAT = 9
ERRORES_REINICIO = 3

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
EN_GITHUB = os.getenv("GITHUB_ACTIONS") == "true"
CAPTURAS.mkdir(exist_ok=True)
# ===================================================

# Inicializamos el solver global de TwoCaptcha con tu clave
solver = TwoCaptcha(TWOCAPTCHA_API_KEY)

def setup_logging():
    handlers = [logging.StreamHandler(sys.stdout)]
    if not EN_GITHUB:
        handlers.append(logging.FileHandler(LOG_FILE, encoding="utf-8"))
    logging.basicConfig(level=logging.INFO, format="[%(asctime)s] %(message)s",
                        datefmt="%Y-%m-%d %H:%M:%S", handlers=handlers)
    return logging.getLogger("monitor")

log = setup_logging()

# ---------------- Telegram ----------------
def telegram(texto, captura=None):
    base = f"https://telegram.org{TELEGRAM_TOKEN}"
    try:
        r = requests.post(f"{base}/sendMessage", data={"chat_id": TELEGRAM_CHAT_ID, "text": texto}, timeout=15)
        if not r.ok:
            log.error(f"Telegram rechazo el mensaje: {r.text}")
        if captura and Path(captura).exists():
            with open(captura, "rb") as f:
                requests.post(f"{base}/sendPhoto", data={"chat_id": TELEGRAM_CHAT_ID}, files={"photo": f}, timeout=30)
    except Exception as e:
        log.error(f"Error Telegram: {e}")

def avisar(titulo, mensaje, captura=None):
    log.info(f"AVISO -> {titulo} | {mensaje}")
    if not EN_GITHUB:
        try:
            from plyer import notification
            notification.notify(title=titulo, message=mensaje[:250], timeout=25)
        except Exception:
            pass
        print("\a" * 4, end="", flush=True)
    telegram(f"{titulo}\n{mensaje}\n{URL}", captura)

def detectar_chat_id():
    try:
        r = requests.get(f"https://telegram.org{TELEGRAM_TOKEN}/getUpdates", timeout=15).json()
        for upd in reversed(r.get("result", [])):
            msg = upd.get("message") or upd.get("edited_message")
            if msg and "chat" in msg:
                return str(msg["chat"]["id"])
    except Exception as e:
        log.error(f"Error detectando chat_id: {e}")
    return ""

def validar_telegram(enviar_prueba=True):
    global TELEGRAM_CHAT_ID
    if not TELEGRAM_TOKEN:
        log.error("Falta TELEGRAM_TOKEN.")
        sys.exit(1)
    if not TELEGRAM_CHAT_ID:
        TELEGRAM_CHAT_ID = detectar_chat_id()
        if not TELEGRAM_CHAT_ID:
            log.error("No encontre tu chat_id.")
            sys.exit(1)
    if enviar_prueba:
        r = requests.post(f"https://telegram.org{TELEGRAM_TOKEN}/sendMessage",
                          data={"chat_id": TELEGRAM_CHAT_ID, "text": "✅ Prueba de conexión con Telegram: OK."}, timeout=15)
        if not r.ok:
            sys.exit(1)

# ---------------- Navegador ----------------
def abrir_navegador(p, headless):
    browser = p.chromium.launch(headless=headless)
    ctx = browser.new_context(locale="es-ES", viewport={"width": 1366, "height": 900})
    page = ctx.new_page()
    page.on("dialog", lambda d: d.accept())
    return browser, page

def contiene(texto, lista):
    return any(t in texto for t in lista)

def revisar(page):
    """Devuelve ('sin_cita' | 'hay_cita' | 'bloqueo' | 'error', detalle)."""
    try:
        page.goto(URL, wait_until="domcontentloaded", timeout=60000)
    except Exception as e:
        return "error", f"Error al cargar: {str(e)[:120]}"

    page.wait_for_timeout(3000)
    texto_actual = page.inner_text("body").lower()

    # DETECCIÓN Y RESOLUCIÓN DEL CAPTCHA / CLOUDFLARE
    if contiene(texto_actual, TEXTOS_BLOQUEO):
        log.info("Captcha/Cloudflare detectado. Solicitando resolución a TwoCaptcha...")
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
            log.info("Token de respuesta recibido de TwoCaptcha de forma exitosa.")
            
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

    # Boton de bienvenida "Continuar"
    for sel in ["#idCaptchaButton", "text=Continuar", "button:has-text('Continuar')"]:
        try:
            btn = page.locator(sel).first
            if btn.is_visible(timeout=5000):
                btn.click()
                page.wait_for_timeout(5000)
                break
        except Exception:
            pass

    if SERVICIO:
        try:
            page.get_by_text(SERVICIO, exact=False).first.click(timeout=15000)
            page.wait_for_timeout(5000)
        except PWTimeout:
            return "error", f"No encontre el servicio '{SERVICIO}'"

    page.wait_for_timeout(3000)
    texto = page.inner_text("body").lower()

    if len(texto.strip()) < 40:
        return "error", "Pagina casi vacia"
    if contiene(texto, TEXTOS_BLOQUEO):
        return "bloqueo", "Sigue bloqueado por CAPTCHA tras intento de bypass"
    if contiene(texto, TEXTOS_SIN_CITA):
        return "sin_cita", "No hay horas disponibles"
    
    return "hay_cita", "¡Posible disponibilidad! El mensaje de 'no hay citas' desapareció"

def una_revision(page):
    try:
        estado, detalle = revisar(page)
    except Exception as e:
        estado, detalle = "error", str(e)[:150]
    captura = None
    if estado in ("hay_cita", "bloqueo", "error", "sin_cita"): # Captura siempre para control visual
        captura = CAPTURAS / f"{dt.datetime.now():%Y%m%d_%H%M%S}_{estado}.png"
        try:
            page.screenshot(path=str(captura), full_page=True)
        except Exception:
            captura = None
    return estado, detalle, captura

# ---------------- Main ----------------
def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--headless", action="store_true", help="Navegador oculto")
    parser.add_argument("--once", action="store_true", help="Una sola revision")
    args = parser.parse_args()

    # Desactivamos envío de prueba inicial para que no sature, el reporte irá al final de la revisión
    validar_telegram(enviar_prueba=False)

    with sync_playwright() as p:
        browser, page = abrir_navegador(p, args.headless)
        
        if args.once:
            estado, detalle, captura = una_revision(page)
            log.info(f"Resultado único: {estado} - {detalle}")
            
            # CONFIGURACIÓN SOLICITADA: Manda Telegram SIEMPRE con el estado actual y foto de la web
            if estado == "hay_cita":
                avisar("🚨 ¡CITAS DISPONIBLES!", f"Estado: {detalle}", captura)
            elif estado == "sin_cita":
                telegram(f"🔍 Revisión automática: El monitor sigue activo y funcionando. {detalle}.", captura)
            elif estado in ("bloqueo", "error"):
                telegram(f"⚠️ Alerta en revisión: {detalle}. Se reintentará en el próximo bloque.", captura)
        else:
            log.info("Iniciando bucle de monitoreo permanente...")
            errores_seguidos = 0
            
            while True:
                estado, detalle, captura = una_revision(page)
                log.info(f"Revisión: {estado} - {detalle}")

                if estado == "hay_cita":
                    avisar("🚨 ¡CITAS DISPONIBLES!", detalle, captura)
                    errores_seguidos = 0
                elif estado in ("error", "bloqueo"):
                    errores_seguidos += 1
                    if errores_seguidos >= ERRORES_REINICIO:
                        telegram(f"⚠️ El monitor lleva {errores_seguidos} fallos seguidos. Último detalle: {detalle}", captura)
