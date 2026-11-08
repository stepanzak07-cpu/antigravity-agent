import os
import sys
import json
import asyncio
import subprocess
from pathlib import Path
from typing import Dict, List, Any

import uvicorn
from fastapi import FastAPI
from fastapi.responses import PlainTextResponse
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

SYSTEM_PROMPT = f"""Ты — полноценный автономный ИИ-агент Antigravity в среде Linux.
Твой создатель и собеседник — SPIDIKSY.

БАЗА ЗНАНИЙ И ПАМЯТЬ О ПОЛЬЗОВАТЕЛЕ:
{knowledge_base}

ТВОИ ВОЗМОЖНОСТИ (TOOLS):
1. execute_bash(command) — выполнение любых консольных команд на сервере (установка пакетов, git clone, запуск Python/Node.js, curl, bash).
2. read_file(path) — чтение файлов из рабочей директории.
3. write_file(path, content) — создание и редактирование файлов.
4. list_directory(path) — просмотр файлов и папок.

ПРАВИЛА АГЕНТА:
- Отвечай строго на русском языке.
- СТРОГО ЗАПРЕЩЕНО использовать шаблонные приветствия («Привет, SPIDIKSY», «На связи» и т.п.). Сразу переходи к делу.
- Ты самостоятельный агент: когда пользователь просит решить задачу, запускай команды, пиши код, проверяй выполнение и возвращай готовый результат.
- Если команда завершилась ошибкой, проанализируй вывод, исправь и запусти снова.
- Рабочая директория по умолчанию: workspace.
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

# Хранилище диалогов в памяти
chat_sessions: Dict[int, List[Dict[str, Any]]] = {}

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

def tool_read_file(path: str) -> str:
    target = (WORKSPACE_DIR / path).resolve()
    try:
        if not target.exists():
            return f"[Ошибка: Файл {path} не существует]"
        return target.read_text(encoding="utf-8")
    except Exception as e:
        return f"[Ошибка чтения файла: {str(e)}]"

def tool_write_file(path: str, content: str) -> str:
    target = (WORKSPACE_DIR / path).resolve()
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
        return f"[Успешно записано в {path} ({len(content)} симв.)]"
    except Exception as e:
        return f"[Ошибка записи в файл: {str(e)}]"

def tool_list_directory(path: str = ".") -> str:
    target = (WORKSPACE_DIR / path).resolve()
    try:
        if not target.exists() or not target.is_dir():
            return f"[Ошибка: Директория {path} не найдена]"
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

    if chat_id not in chat_sessions:
        chat_sessions[chat_id] = []
    
    # Работаем с локальной копией, чтобы при ошибке не повреждать историю сессии
    history = list(chat_sessions[chat_id])
    history.append({
        "role": "user",
        "parts": [{"text": user_message}]
    })

    if len(history) > 20:
        history = history[-20:]

    endpoint = f"https://generativelanguage.googleapis.com/v1beta/models/{MODEL_NAME}:generateContent?key={GEMINI_API_KEY}"

    max_steps = 10
    step = 0

    while step < max_steps:
        step += 1
        payload = {
            "systemInstruction": {
                "parts": [{"text": SYSTEM_PROMPT}]
            },
            "contents": history,
            "tools": [{"functionDeclarations": TOOL_DECLARATIONS}],
            "generationConfig": {
                "temperature": 0.3,
                "maxOutputTokens": 2048
            }
        }

        async with aiohttp.ClientSession() as http:
            async with http.post(endpoint, json=payload) as resp:
                if resp.status != 200:
                    err_body = await resp.text()
                    raise Exception(f"Google API {resp.status}: {err_body}")
                data = await resp.json()

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
            
            history.append({
                "role": "model",
                "parts": [{"text": final_text}]
            })
            chat_sessions[chat_id] = history
            return final_text

        # Сохраняем ход модели с вызовами функций
        history.append({
            "role": "model",
            "parts": parts
        })

        # Собираем ответы ВСЕХ вызванных функций в ОДИН ход (требование Google API)
        response_parts = []
        for fc in tool_calls:
            fn_name = fc.get("name")
            fn_args = fc.get("args", {})

            if fn_name == "execute_bash":
                cmd_preview = fn_args.get("command", "")[:120]
                await bot.send_chat_action(chat_id, "typing")
                try:
                    await bot.send_message(chat_id, f"⚙️ *Команда:* `{cmd_preview}`", parse_mode="Markdown")
                except Exception:
                    await bot.send_message(chat_id, f"⚙️ Команда: {cmd_preview}")

            tool_output = run_tool(fn_name, fn_args)

            response_parts.append({
                "functionResponse": {
                    "name": fn_name,
                    "response": {"output": tool_output}
                }
            })

        # Добавляем единый ход с результатами функций
        history.append({
            "role": "user",
            "parts": response_parts
        })

    chat_sessions[chat_id] = history
    return "⚠️ Превышен лимит шагов агента при решении задачи."

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
    if ALLOWED_USER_ID and str(message.from_user.id) != ALLOWED_USER_ID:
        await message.answer(f"⛔ Доступ запрещен. Ваш ID: {message.from_user.id}")
        return

    chat_id = message.chat.id
    user_text = message.text.strip()

    await bot.send_chat_action(chat_id, "typing")

    try:
        response = await run_agent_loop(chat_id, user_text, bot)
        for i in range(0, len(response), 4000):
            chunk = response[i:i+4000]
            try:
                await message.answer(chunk, parse_mode="Markdown")
            except Exception:
                await message.answer(chunk)
    except Exception as e:
        print(f"Ошибка в Agent Loop: {e}")
        await message.answer(f"⚠️ Ошибка агента:\n{str(e)[:500]}")

# FastAPI веб-сервер для Render Health Check
app = FastAPI()

@app.get("/")
def read_root():
    return PlainTextResponse("✅ Antigravity Linux Agent is running 24/7 on Render!")

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
    await asyncio.sleep(60) # ждем 1 минуту после старта
    url = RENDER_EXTERNAL_URL
    if not url:
        return
    while True:
        try:
            async with aiohttp.ClientSession() as s:
                async with s.get(url, timeout=10) as r:
                    print(f"Keep-Alive ping {url} -> {r.status}")
        except Exception as e:
            print(f"Keep-Alive ping error: {e}")
        await asyncio.sleep(540) # пинг каждые 9 минут

async def start_telegram():
    if not TELEGRAM_BOT_TOKEN:
        print("ВНИМАНИЕ: TELEGRAM_BOT_TOKEN не задан! Бот не запущен.")
        return
    # Сбрасываем вебхуки, чтобы работал быстрый Long Polling
    await bot.delete_webhook(drop_pending_updates=True)
    print("Вебхук очищен. Запуск Long Polling...")
    await dp.start_polling(bot)

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
