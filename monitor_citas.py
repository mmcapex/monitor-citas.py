"""
Monitor de citas - citaconsular.es (widget Bookitit)
Solo avisa por Telegram. No resuelve ni evade CAPTCHAs / Cloudflare.

Instalacion (una sola vez, PowerShell):
    py -m pip install playwright requests plyer
    py -m playwright install chromium

Credenciales (una sola vez, PowerShell; luego cierra y abre PowerShell):
    setx TELEGRAM_TOKEN "tu_token"
    setx TELEGRAM_CHAT_ID "7759185260"      # opcional: si falta, se detecta solo

Uso:
    py monitor_citas.py                 # bucle local, navegador visible
    py monitor_citas.py --headless      # bucle local, sin ventana
    py monitor_citas.py --once --headless   # una revision y termina (GitHub Actions)

Como saber si funciona:
    - Telegram: "Sigo vivo" todos los dias a las 9:00 con el resumen del dia.
    - Archivo monitor_citas.log en la misma carpeta: una linea por revision.
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

# ================== CONFIGURACION ==================
URL = "https://www.citaconsular.es/es/hosteds/widgetdefault/2d8bebcf444f3db762074e5daef723a59/#services"

# Texto (o parte) del servicio a seleccionar, p.ej. "Pasaporte". Vacio = no selecciona.
SERVICIO = ""

# Espera aleatoria entre revisiones (minutos)
MIN_ESPERA = 35
MAX_ESPERA = 70

# Si hay bloqueo / Cloudflare, usar
result = solver.turnstile(sitekey='0x1AAAAAAAAkg0s2VIOD34y5',
                            url='http://mysite.com/', 
                            data='foo',
                            pagedata='bar',
                            action='challenge',
                            useragent='Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/114.0.0.0 Safari/537.36')

# Tope de revisiones por dia (proteccion)
MAX_REVISIONES_DIA = 28

# Resumen diario "Sigo vivo" (hora local de la computadora)
HORA_HEARTBEAT = 9

# Reiniciar el navegador tras N errores seguidos; avisar por Telegram tras N errores
ERRORES_REINICIO = 3

TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN", "")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "")

TEXTOS_SIN_CITA = [
    "no hay horas disponibles",
    "no hay citas disponibles",
    "no available hours",
    "no hours available",
    "no hay horas",
    "sin citas",
    "no se han encontrado",
]
TEXTOS_BLOQUEO = [
    "captcha",
    "acceso denegado",
    "access denied",
    "too many requests",
    "demasiadas solicitudes",
    "verify you are human",
    "verificación de seguridad",
    "verificacion de seguridad",
    "security verification",
    "just a moment",
    "verificando",
    "cloudflare",
]

CAPTURAS = Path("capturas_citas")
LOG_FILE = Path("monitor_citas.log")
EN_GITHUB = os.getenv("GITHUB_ACTIONS") == "true"
# ===================================================


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
    base = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}"
    try:
        r = requests.post(f"{base}/sendMessage",
                          data={"chat_id": TELEGRAM_CHAT_ID, "text": texto}, timeout=15)
        if not r.ok:
            log.error(f"Telegram rechazo el mensaje: {r.text}")
        if captura and Path(captura).exists():
            with open(captura, "rb") as f:
                requests.post(f"{base}/sendPhoto", data={"chat_id": TELEGRAM_CHAT_ID},
                              files={"photo": f}, timeout=30)
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
        r = requests.get(f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/getUpdates",
                         timeout=15).json()
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
        log.error("Falta TELEGRAM_TOKEN. En PowerShell: setx TELEGRAM_TOKEN \"tu_token\" "
                  "y vuelve a abrir PowerShell.")
        sys.exit(1)
    if not TELEGRAM_CHAT_ID:
        TELEGRAM_CHAT_ID = detectar_chat_id()
        if not TELEGRAM_CHAT_ID:
            log.error("No encontre tu chat_id. Escribe 'hola' a tu bot y vuelve a correr el script.")
            sys.exit(1)
        log.info(f"chat_id detectado: {TELEGRAM_CHAT_ID}")
    if enviar_prueba:
        r = requests.post(f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage",
                          data={"chat_id": TELEGRAM_CHAT_ID,
                                "text": "✅ Monitor de citas iniciado. Te aviso aqui si hay horas."},
                          timeout=15)
        if not r.ok:
            log.error(f"Telegram rechazo el mensaje de prueba: {r.text}")
            sys.exit(1)
        log.info("Telegram OK")


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

    # Verificacion de Cloudflare: esperar hasta 30 s a que se resuelva sola
    for _ in range(15):
        page.wait_for_timeout(2000)
        if not contiene(page.inner_text("body").lower(), TEXTOS_BLOQUEO):
            break
    else:
        return "bloqueo", "Cloudflare / verificacion de seguridad no dejo pasar"
    page.wait_for_timeout(2000)

    # Boton de bienvenida "Continuar" (no resuelve captchas)
    for sel in ["#idCaptchaButton", "text=Continuar", "text=Continue",
                "button:has-text('Continuar')"]:
        try:
            btn = page.locator(sel).first
            if btn.is_visible(timeout=2500):
                btn.click()
                page.wait_for_timeout(3000)
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
        return "bloqueo", "CAPTCHA o bloqueo - requiere intervencion manual"
    if contiene(texto, TEXTOS_SIN_CITA):
        return "sin_cita", "No hay horas disponibles"
    return "hay_cita", "Posible disponibilidad (no aparece el mensaje de 'sin horas')"


def una_revision(page):
    try:
        estado, detalle = revisar(page)
    except Exception as e:
        estado, detalle = "error", str(e)[:150]
    captura = None
    if estado in ("hay_cita", "bloqueo", "error"):
        captura = CAPTURAS / f"{dt.datetime.now():%Y%m%d_%H%M%S}_{estado}.png"
        try:
            page.screenshot(path=str(captura), full_page=True)
        except Exception:
            captura = None
    return estado, detalle, captura


# ---------------- Main ----------------
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--headless", action="store_true", help="sin ventana")
    ap.add_argument("--once", action="store_true", help="una revision y termina (GitHub)")
    args = ap.parse_args()

    CAPTURAS.mkdir(exist_ok=True)
    validar_telegram(enviar_prueba=not args.once
                     or os.getenv("GITHUB_EVENT_NAME") == "workflow_dispatch")

    with sync_playwright() as p:
        browser, page = abrir_navegador(p, args.headless)

        # ---- Modo GitHub: una revision ----
        if args.once:
            estado, detalle, captura = una_revision(page)
            log.info(f"{estado.upper()} | {detalle}")
            if estado == "hay_cita":
                avisar("¡Posible cita disponible!", detalle, captura)
            elif estado in ("bloqueo", "error") and os.getenv("GITHUB_EVENT_NAME") == "workflow_dispatch":
                # En corridas programadas no se avisa cada bloqueo (seria spam cada hora);
                # se reporta en el "Sigo vivo" diario. En prueba manual si se avisa.
                avisar(f"Prueba manual: {estado}", detalle, captura)
            if dt.datetime.utcnow().hour == HORA_HEARTBEAT + 5:  # 9 am Cancun = 14 UTC
                telegram(f"🟢 Sigo vivo. Ultima revision: {estado} - {detalle}")
            return

        # ---- Modo local: bucle ----
        dia = dt.date.today()
        stats = {"sin_cita": 0, "hay_cita": 0, "bloqueo": 0, "error": 0}
        revisiones_hoy = 0
        heartbeat_enviado = None
        ultimo = None
        errores = 0

        log.info(f"Monitor iniciado | intervalo {MIN_ESPERA}-{MAX_ESPERA} min | "
                 f"max {MAX_REVISIONES_DIA}/dia | Ctrl+C para detener")

        while True:
            ahora = dt.datetime.now()

            # Resumen diario
            if ahora.hour >= HORA_HEARTBEAT and heartbeat_enviado != ahora.date():
                telegram(f"🟢 Sigo vivo. Ultimas 24 h: {sum(stats.values())} revisiones | "
                         f"sin cita {stats['sin_cita']} | bloqueos {stats['bloqueo']} | "
                         f"errores {stats['error']} | ultimo estado: {ultimo or 'n/a'}")
                heartbeat_enviado = ahora.date()
                stats = dict.fromkeys(stats, 0)

            # Contador diario
            if ahora.date() != dia:
                dia, revisiones_hoy = ahora.date(), 0
            if revisiones_hoy >= MAX_REVISIONES_DIA:
                log.info("Limite diario alcanzado; espero 1 h")
                time.sleep(3600)
                continue

            estado, detalle, captura = una_revision(page)
            revisiones_hoy += 1
            stats[estado] += 1
            log.info(f"{estado.upper()} | {detalle} | revision {revisiones_hoy}/{MAX_REVISIONES_DIA}")

            # Avisos: cita SIEMPRE; bloqueo solo cuando cambia; errores al 3o seguido
            if estado == "hay_cita":
                avisar("¡Posible cita disponible!", detalle, captura)
            elif estado == "bloqueo" and ultimo != "bloqueo":
                avisar("Monitor bloqueado / CAPTCHA", detalle, captura)

            if estado == "error":
                errores += 1
                if errores == ERRORES_REINICIO:
                    avisar("Monitor con errores", f"{errores} errores seguidos: {detalle}", captura)
                    log.warning("Reiniciando navegador")
                    try:
                        browser.close()
                    except Exception:
                        pass
                    browser, page = abrir_navegador(p, args.headless)
            else:
                errores = 0
            ultimo = estado

            if estado == "bloqueo":
                espera = COOLDOWN_BLOQUEO * 60
            else:
                espera = random.randint(MIN_ESPERA * 60, MAX_ESPERA * 60)
            log.info(f"Proxima revision: {dt.datetime.now() + dt.timedelta(seconds=espera):%H:%M:%S}")
            time.sleep(espera)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        log.info("Detenido por el usuario.")
