import os
import sys
import json
import asyncio
import subprocess
from pathlib import Path
from typing import Dict, List, Any

import uvicorn
from fastapi import FastAPI, Request
from fastapi.responses import PlainTextResponse, FileResponse
from fastapi.staticfiles import StaticFiles
from aiogram import Bot, Dispatcher, types
from aiogram.filters import CommandStart
import aiohttp

# Конфигурация из переменных окружения
PORT = int(os.getenv("PORT", 10000))
TELEGRAM_BOT_TOKEN = (os.getenv("TELEGRAM_BOT_TOKEN") or "").strip()
GEMINI_API_KEY = (os.getenv("GEMINI_API_KEY") or os.getenv("EMINI_API_KEY") or "").strip()
ALLOWED_USER_ID = (os.getenv("ALLOWED_USER_ID") or "").strip()
RENDER_EXTERNAL_URL = (os.getenv("RENDER_EXTERNAL_URL") or "").strip()
MODEL_NAME = (os.getenv("GEMINI_MODEL") or "gemini-3.5-flash-lite").strip()

# Рабочая директория агента
WORKSPACE_DIR = (Path(__file__).parent / "workspace").resolve()
WORKSPACE_DIR.mkdir(parents=True, exist_ok=True)

# Загрузка базы знаний (AGENTS.md)
AGENTS_MD_PATH = WORKSPACE_DIR / "AGENTS.md"
knowledge_base = ""
if AGENTS_MD_PATH.exists():
    try:
        knowledge_base = AGENTS_MD_PATH.read_text(encoding="utf-8")
    except Exception as e:
        print(f"Ошибка чтения AGENTS.md: {e}")

SYSTEM_PROMPT = f"""Ты — полноценный автономный ИИ-агент Antigravity в среде Linux (Render).
Твой создатель и собеседник — SPIDIKSY.

БАЗА ЗНАНИЙ И ПАМЯТЬ О ПОЛЬЗОВАТЕЛЕ:
{knowledge_base}

ТВОИ ВОЗМОЖНОСТИ (TOOLS):
1. execute_bash(command) — выполнение консольных команд на сервере (запуск Python/Node.js, curl, pip).
2. read_file(path) — чтение файлов из рабочей директории workspace.
3. write_file(path, content) — создание и редактирование файлов.
4. list_directory(path) — просмотр файлов и папок.

ВАЖНЕЙШИЕ ПРАВИЛА:
1. Отвечай строго на русском языке, без шаблонных приветствий («Привет, SPIDIKSY», «На связи» строго запрещены).
2. ВЕБ-СЕРВЕР И САЙТЫ: Ты уже работаешь внутри боевого веб-сервера. Любой файл, созданный в папке workspace, АВТОМАТИЧЕСКИ доступен по ссылке:
   https://antigravity-agent-mz65.onrender.com/workspace/<имя_файла>
   ТЕБЕ НЕ НУЖНО запускать второй сервер через python http.server или nohup! Просто запиши HTML/CSS файл через write_file и пришли эту ссылку пользователю!
3. ТОЧНОСТЬ И ЭКОНОМИЯ ДЕЙСТВИЙ: Решай задачу кратчайшим путём за 1-2 шага. Не выполняй лишних команд (не вызывай git status, ps aux, ls, если пользователь не просил диагностику системы).
4. ОСТАНОВКА: Как только целевое действие сделано (файл записан или скрипт выполнен и дал результат) — НЕМЕДЛЕННО ОСТАНОВИСЬ, прекрати вызывать инструменты и напиши итоговый ответ пользователю в Telegram с результатами!
"""

# Описание инструментов для Gemini Function Calling
TOOL_DECLARATIONS = [
    {
        "name": "execute_bash",
        "description": "Выполнить bash-команду в терминале Linux сервера и получить результат (stdout/stderr).",
        "parameters": {
            "type": "OBJECT",
            "properties": {
                "command": {
                    "type": "STRING",
                    "description": "Команда для выполнения в консоли (например, 'ls -la', 'python script.py', 'pip install requests')"
                }
            },
            "required": ["command"]
        }
    },
    {
        "name": "read_file",
        "description": "Прочитать содержимое файла в рабочей директории.",
        "parameters": {
            "type": "OBJECT",
            "properties": {
                "path": {
                    "type": "STRING",
                    "description": "Относительный или абсолютный путь к файлу."
                }
            },
            "required": ["path"]
        }
    },
    {
        "name": "write_file",
        "description": "Создать или перезаписать файл с указанным содержимым.",
        "parameters": {
            "type": "OBJECT",
            "properties": {
                "path": {
                    "type": "STRING",
                    "description": "Путь к файлу."
                },
                "content": {
                    "type": "STRING",
                    "description": "Текст или исходный код для записи."
                }
            },
            "required": ["path", "content"]
        }
    },
    {
        "name": "list_directory",
        "description": "Получить список файлов и подкаталогов в указанной папке.",
        "parameters": {
            "type": "OBJECT",
            "properties": {
                "path": {
                    "type": "STRING",
                    "description": "Путь к каталогу (по умолчанию .)"
                }
            }
        }
    }
]

# Хранилище диалогов и блокировок в памяти
chat_sessions: Dict[int, List[Dict[str, Any]]] = {}
chat_locks: Dict[int, asyncio.Lock] = {}

def get_chat_lock(chat_id: int) -> asyncio.Lock:
    if chat_id not in chat_locks:
        chat_locks[chat_id] = asyncio.Lock()
    return chat_locks[chat_id]

# Реализация инструментов
def tool_execute_bash(command: str) -> str:
    print(f"==> EXEC BASH: {command}")
    try:
        proc = subprocess.run(
            command,
            shell=True,
            cwd=str(WORKSPACE_DIR),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=120
        )
        out = proc.stdout.strip()
        err = proc.stderr.strip()
        result = []
        if out:
            result.append(f"[STDOUT]\n{out}")
        if err:
            result.append(f"[STDERR]\n{err}")
        result.append(f"[Exit code: {proc.returncode}]")
        return "\n".join(result) if result else "[Команда успешно выполнена без вывода]"
    except subprocess.TimeoutExpired:
        return "[Ошибка: Превышен тайм-аут выполнения команды (120 секунд)]"
    except Exception as e:
        return f"[Ошибка выполнения bash: {str(e)}]"

def resolve_workspace_path(path: str) -> Path:
    p = Path(path.strip())
    parts = list(p.parts)
    while parts and parts[0] in ["/", "\\", "app", "workspace"]:
        parts.pop(0)
    clean_p = Path(*parts) if parts else Path("index.html")
    return (WORKSPACE_DIR / clean_p).resolve()

def tool_read_file(path: str) -> str:
    target = resolve_workspace_path(path)
    try:
        if not target.exists():
            return f"[Ошибка: Файл {path} не существует]"
        return target.read_text(encoding="utf-8")
    except Exception as e:
        return f"[Ошибка чтения файла: {str(e)}]"

def tool_write_file(path: str, content: str) -> str:
    target = resolve_workspace_path(path)
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
        return f"[Успешно записано в {target.name} ({len(content)} симв.)]"
    except Exception as e:
        return f"[Ошибка записи в файл: {str(e)}]"

def tool_list_directory(path: str = ".") -> str:
    target = resolve_workspace_path(path)
    try:
        if not target.exists() or not target.is_dir():
            target = WORKSPACE_DIR
        items = os.listdir(target)
        res = []
        for it in sorted(items):
            p = target / it
            res.append(f"{'[DIR] ' if p.is_dir() else '[FILE]'} {it}")
        return "\n".join(res) if res else "[Директория пуста]"
    except Exception as e:
        return f"[Ошибка list_directory: {str(e)}]"

def run_tool(name: str, args: Dict[str, Any]) -> str:
    if name == "execute_bash":
        return tool_execute_bash(args.get("command", ""))
    elif name == "read_file":
        return tool_read_file(args.get("path", ""))
    elif name == "write_file":
        return tool_write_file(args.get("path", ""), args.get("content", ""))
    elif name == "list_directory":
        return tool_list_directory(args.get("path", "."))
    return f"[Ошибка: Неизвестный инструмент {name}]"

# Агентный цикл общения с Gemini
async def run_agent_loop(chat_id: int, user_message: str, bot: Bot) -> str:
    if not GEMINI_API_KEY:
        raise Exception("Переменная GEMINI_API_KEY не найдена в Environment сервиса Render! Добавьте её в настройках Environment.")

    # Очищаем историю сессии от возможных сломанных промежуточных вызовов
    clean_history = []
    for turn in chat_sessions.get(chat_id, []):
        if "role" in turn and "parts" in turn:
            text_parts = [p for p in turn["parts"] if "text" in p and p.get("text", "").strip()]
            if text_parts:
                clean_history.append({"role": turn["role"], "parts": text_parts})

    # Ограничиваем историю 10 последними сообщениями для стабильности
    if len(clean_history) > 10:
        clean_history = clean_history[-10:]

    # Локальная рабочая цепочка шагов для текущей задачи
    current_turns = list(clean_history)
    current_turns.append({
        "role": "user",
        "parts": [{"text": user_message}]
    })

    endpoint = f"https://generativelanguage.googleapis.com/v1beta/models/{MODEL_NAME}:generateContent?key={GEMINI_API_KEY}"

    max_steps = 15
    step = 0

    while step < max_steps:
        step += 1
        payload = {
            "systemInstruction": {
                "parts": [{"text": SYSTEM_PROMPT}]
            },
            "contents": current_turns,
            "tools": [{"functionDeclarations": TOOL_DECLARATIONS}],
            "generationConfig": {
                "temperature": 0.3,
                "maxOutputTokens": 8192
            }
        }

        data = None
        for attempt in range(5):
            async with aiohttp.ClientSession() as http:
                async with http.post(endpoint, json=payload) as resp:
                    if resp.status == 429:
                        wait_sec = 6 + (attempt * 3)
                        print(f"⚠️ 429 Rate Limit (попытка {attempt+1}/5). Ожидаем {wait_sec} сек...")
                        await bot.send_chat_action(chat_id, "typing")
                        await asyncio.sleep(wait_sec)
                        continue
                    if resp.status != 200:
                        err_body = await resp.text()
                        raise Exception(f"Google API {resp.status}: {err_body}")
                    data = await resp.json()
                    break

        if not data:
            raise Exception("Google API 429: Лимит запросов в минуту исчерпан, подождите 30 секунд.")

        candidate = data.get("candidates", [{}])[0]
        content = candidate.get("content", {})
        parts = content.get("parts", [])

        tool_calls = [p["functionCall"] for p in parts if "functionCall" in p]

        if not tool_calls:
            raw_text = "".join([p.get("text", "") for p in parts if "text" in p]).strip()
            clean_text = raw_text
            for prefix in ["Привет, SPIDIKSY. На связи.", "Привет, SPIDIKSY.", "На связи."]:
                if clean_text.lower().startswith(prefix.lower()):
                    clean_text = clean_text[len(prefix):].strip()
            final_text = clean_text or raw_text
            
            # Сохраняем в постоянную память ТОЛЬКО чистый текст вопроса и ответа
            chat_sessions[chat_id] = clean_history + [
                {"role": "user", "parts": [{"text": user_message}]},
                {"role": "model", "parts": [{"text": final_text}]}
            ]
            return final_text

        # Сохраняем ход модели с вызовами функций в текущую локальную цепочку
        current_turns.append({
            "role": "model",
            "parts": parts
        })

        # Собираем ответы ВСЕХ вызванных функций в ОДИН ход (требование Google API)
        response_parts = []
        for fc in tool_calls:
            fn_name = fc.get("name")
            fn_args = fc.get("args", {})

            await bot.send_chat_action(chat_id, "typing")
            if fn_name == "execute_bash":
                cmd_preview = fn_args.get("command", "")[:120]
                try:
                    await bot.send_message(chat_id, f"⚙️ *Команда:* `{cmd_preview}`", parse_mode="Markdown")
                except Exception:
                    await bot.send_message(chat_id, f"⚙️ Команда: {cmd_preview}")
            elif fn_name == "write_file":
                path_preview = fn_args.get("path", "")
                try:
                    await bot.send_message(chat_id, f"📝 *Создаю/пишу файл:* `{path_preview}`...", parse_mode="Markdown")
                except Exception:
                    await bot.send_message(chat_id, f"📝 Создаю/пишу файл: {path_preview}...")
            elif fn_name == "read_file":
                path_preview = fn_args.get("path", "")
                try:
                    await bot.send_message(chat_id, f"📖 *Читаю файл:* `{path_preview}`...", parse_mode="Markdown")
                except Exception:
                    await bot.send_message(chat_id, f"📖 Читаю файл: {path_preview}...")

            tool_output = run_tool(fn_name, fn_args)

            response_parts.append({
                "functionResponse": {
                    "name": fn_name,
                    "response": {"output": tool_output}
                }
            })

        # Добавляем единый ход с результатами функций
        current_turns.append({
            "role": "user",
            "parts": response_parts
        })

    # Если шаги исчерпаны, просим модель подвести итог без вызова новых инструментов
    try:
        final_payload = {
            "systemInstruction": {"parts": [{"text": SYSTEM_PROMPT}]},
            "contents": current_turns + [{
                "role": "user",
                "parts": [{"text": "Шаги выполнения завершены. Напиши итоговый отчёт для пользователя: что было сделано и какой получился результат."}]
            }],
            "generationConfig": {"temperature": 0.3, "maxOutputTokens": 2048}
        }
        async with aiohttp.ClientSession() as http:
            async with http.post(endpoint, json=final_payload) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    parts = data.get("candidates", [{}])[0].get("content", {}).get("parts", [])
                    final_text = "".join([p.get("text", "") for p in parts if "text" in p]).strip()
                    if final_text:
                        chat_sessions[chat_id] = clean_history + [
                            {"role": "user", "parts": [{"text": user_message}]},
                            {"role": "model", "parts": [{"text": final_text}]}
                        ]
                        return final_text
    except Exception:
        pass

    return "✅ Задача выполнена на сервере. Результаты сохранены в workspace."

# Telegram Bot
bot = Bot(token=TELEGRAM_BOT_TOKEN) if TELEGRAM_BOT_TOKEN else None
dp = Dispatcher()

@dp.message(CommandStart())
async def handle_start(message: types.Message):
    if ALLOWED_USER_ID and str(message.from_user.id) != ALLOWED_USER_ID:
        await message.answer(f"⛔ Доступ запрещен. Ваш ID: {message.from_user.id}")
        return
    chat_sessions[message.chat.id] = []
    await message.answer(
        "⚡ **Автономный Linux-агент Antigravity запущен на Render!**\n\n"
        "У меня есть доступ к терминалу bash, установщику пакетов и файлам.\n"
        "Отправляй любую задачу — напишу код, протестирую в консоли и пришлю отчёт.",
        parse_mode="Markdown"
    )

@dp.message(lambda msg: msg.text in ["/reset", "/clear", "сброс", "очисти диалог"])
async def handle_reset(message: types.Message):
    chat_sessions[message.chat.id] = []
    await message.answer("🧹 История диалога очищена. Можем начинать новую задачу с чистого листа.")

@dp.message()
async def handle_message(message: types.Message):
    if not message.text:
        return
    print(f"📥 ВХОДЯЩЕЕ СООБЩЕНИЕ от {message.from_user.id}: {message.text[:100]}")
    
    if ALLOWED_USER_ID and str(message.from_user.id) != ALLOWED_USER_ID:
        print(f"⛔ Отклонён доступ для ID: {message.from_user.id}")
        await message.answer(f"⛔ Доступ запрещен. Ваш ID: {message.from_user.id}")
        return

    chat_id = message.chat.id
    user_text = message.text.strip()

    await bot.send_chat_action(chat_id, "typing")

    async with get_chat_lock(chat_id):
        try:
            response = await run_agent_loop(chat_id, user_text, bot)
            for i in range(0, len(response), 4000):
                chunk = response[i:i+4000]
                try:
                    await message.answer(chunk, parse_mode="Markdown")
                except Exception:
                    await message.answer(chunk)
        except Exception as e:
            import traceback
            err_msg = traceback.format_exc()
            print(f"Ошибка в Agent Loop:\n{err_msg}")
            await message.answer(f"⚠️ Ошибка агента:\n{str(e)[:500]}")

# FastAPI веб-сервер для Render
app = FastAPI()

@app.get("/workspace/{file_path:path}")
async def get_workspace_file(file_path: str):
    clean = file_path.strip().lstrip("/")
    candidates = [
        WORKSPACE_DIR / clean,
        WORKSPACE_DIR / clean.removeprefix("workspace/"),
        WORKSPACE_DIR / clean.removeprefix("workspace/workspace/"),
        WORKSPACE_DIR / "workspace" / clean,
    ]
    for c in candidates:
        if c.is_file():
            return FileResponse(c)
        if c.is_dir() and (c / "index.html").is_file():
            return FileResponse(c / "index.html")
    return PlainTextResponse(f"File '{file_path}' not found in workspace", status_code=404)

@app.get("/")
def read_root():
    # Если в workspace есть index.html, отдаем его по корню!
    index_file = WORKSPACE_DIR / "index.html"
    if index_file.is_file():
        return FileResponse(index_file)
    return PlainTextResponse("✅ Antigravity Linux Agent is running 24/7 on Render!")

# Быстрый и надёжный Webhook для Telegram (без конфликтов и сбоев)
@app.post("/webhook")
async def telegram_webhook(request: Request):
    try:
        data = await request.json()
        update = types.Update.model_validate(data, context={"bot": bot})
        asyncio.create_task(dp.feed_update(bot, update))
    except Exception as e:
        print(f"Ошибка в webhook handler: {e}")
    return PlainTextResponse("OK")

@app.get("/set-webhook")
async def trigger_set_webhook():
    if not RENDER_EXTERNAL_URL:
        return PlainTextResponse("RENDER_EXTERNAL_URL не задан!")
    webhook_url = f"{RENDER_EXTERNAL_URL}/webhook"
    res = await bot.set_webhook(webhook_url, drop_pending_updates=False)
    return PlainTextResponse(f"Webhook set to {webhook_url}: {res}")

@app.get("/test-gemini")
async def test_gemini(msg: str = "Привет"):
    try:
        class DummyBot:
            async def send_chat_action(self, *args, **kwargs): pass
            async def send_message(self, *args, **kwargs): pass
        res = await run_agent_loop(999999, msg, DummyBot())
        return PlainTextResponse(f"Agent Loop Success: {res}")
    except Exception as e:
        import traceback
        return PlainTextResponse(f"Agent Loop Error:\n{traceback.format_exc()}", status_code=500)

# Фоновая задача Keep-Alive (защита от засыпания)
async def keep_alive_loop():
    await asyncio.sleep(30)
    url = RENDER_EXTERNAL_URL or "https://antigravity-agent-mz65.onrender.com"
    while True:
        try:
            async with aiohttp.ClientSession() as s:
                async with s.get(url, timeout=10) as r:
                    print(f"Keep-Alive ping {url} -> {r.status}")
        except Exception as e:
            print(f"Keep-Alive ping error: {e}")
        await asyncio.sleep(540)

async def start_telegram():
    if not TELEGRAM_BOT_TOKEN:
        print("ВНИМАНИЕ: TELEGRAM_BOT_TOKEN не задан! Бот не запущен.")
        return

    # Привязываем надёжный Webhook через публичный домен Render
    url = RENDER_EXTERNAL_URL or "https://antigravity-agent-mz65.onrender.com"
    webhook_url = f"{url}/webhook"
    try:
        print(f"🚀 Устанавливаем Webhook на {webhook_url}...")
        await bot.set_webhook(webhook_url, drop_pending_updates=False)
        print("✅ Telegram Webhook успешно активирован!")
    except Exception as e:
        print(f"⚠️ Ошибка установки вебхука ({e}). Запускаем аварийный Long Polling...")
        while True:
            try:
                await bot.delete_webhook(drop_pending_updates=False)
                await dp.start_polling(bot, handle_as_tasks=True)
            except Exception as pe:
                print(f"Polling error: {pe}")
                await asyncio.sleep(3)

async def main():
    config = uvicorn.Config(app, host="0.0.0.0", port=PORT, log_level="info")
    server = uvicorn.Server(config)
    
    await asyncio.gather(
        server.serve(),
        start_telegram(),
        keep_alive_loop()
    )

if __name__ == "__main__":
    asyncio.run(main())
