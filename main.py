"""
Точка входа для запуска CS2 Skin Trader Bot на сервере Render (24/7).
Включает:
1. Запуск Telegram-бота (@CS2Market435_bot)
2. Встроенный легковесный HTTP-сервер для прохождения Render Health Check ($PORT)
3. Автоматический Keep-Alive Self-Ping (каждые 9 минут) против засыпания Render Free
"""

import asyncio
import http.server
import logging
import os
import socketserver
import threading
import time
import httpx
from dotenv import load_dotenv

load_dotenv()
from src.bot import CS2TelegramBot

logging.basicConfig(format="%(asctime)s [%(levelname)s] %(message)s", level=logging.INFO)
log = logging.getLogger("render_app")


class HealthHandler(http.server.SimpleHTTPRequestHandler):
    """Легковесный HTTP-обработчик для Health Check от Render"""
    def do_GET(self):
        self.send_response(200)
        self.send_header("Content-type", "application/json")
        self.end_headers()
        self.wfile.write(b'{"status": "ok", "service": "CS2 Market Sniper Bot"}')

    def log_message(self, format, *args):
        # Отключаем спам в логи от health check
        return


def start_health_server(port: int = 10000):
    """Запуск HTTP-сервера в отдельном потоке"""
    handler = HealthHandler
    with socketserver.TCPServer(("0.0.0.0", port), handler) as httpd:
        log.info(f"✅ Render Health Server запущен на порту {port}")
        httpd.serve_forever()


def keep_alive_worker(service_url: str):
    """Фоновый пинг для защиты от засыпания Render Free (каждые 9 минут)"""
    time.sleep(60)  # Даем сервису подняться
    log.info(f"⏰ Запущена служба Keep-Alive для URL: {service_url}")
    while True:
        try:
            with httpx.Client(timeout=10.0) as client:
                resp = client.get(service_url)
                log.info(f"🏓 Self-Ping {service_url}: HTTP {resp.status_code}")
        except Exception as e:
            log.warning(f"Ошибка Self-Ping: {e}")
        time.sleep(540)  # 9 минут


def main():
    token = os.getenv("TELEGRAM_BOT_TOKEN")
    chat_id = int(os.getenv("TELEGRAM_CHAT_ID", "7198803917"))
    port = int(os.getenv("PORT", "10000"))
    render_url = os.getenv("RENDER_EXTERNAL_URL")

    if not token:
        log.error("TELEGRAM_BOT_TOKEN не задан в .env!")
        return

    # 1. Запуск Health HTTP сервера в фоне
    http_thread = threading.Thread(target=start_health_server, args=(port,), daemon=True)
    http_thread.start()

    # 2. Если задан Render URL - запуск Keep-Alive пинга
    if render_url:
        ping_thread = threading.Thread(target=keep_alive_worker, args=(render_url,), daemon=True)
        ping_thread.start()

    # 3. Запуск Telegram-бота
    log.info("🚀 Запуск CS2 Telegram Bot...")
    bot = CS2TelegramBot(token=token, chat_id=chat_id, budget=200.0, dry_run=True)
    bot.run_polling()


if __name__ == "__main__":
    main()
