import os
import json
import asyncio
import time
import random
import re
import ssl
import certifi
import base64
import aiohttp
from io import BytesIO
from pathlib import Path
from datetime import datetime
from zoneinfo import ZoneInfo
from dotenv import load_dotenv

from aiogram import Bot, Dispatcher, F
from aiogram.types import Message, CallbackQuery, BotCommand, InlineKeyboardButton, InlineKeyboardMarkup, Update, InlineQuery, InlineQueryResultArticle, InputTextMessageContent
from aiogram.filters import CommandStart, Command
from aiogram.utils.keyboard import InlineKeyboardBuilder
from aiogram.dispatcher.middlewares.base import BaseMiddleware

env_path = Path(__file__).parent / ".env"
load_dotenv(dotenv_path=env_path)

BOT_TOKEN = os.getenv("BOT_TOKEN")
DEEPSEEK_API_KEY = os.getenv("DEEPSEEK_API_KEY")
DEEPSEEK_BASE_URL = os.getenv("DEEPSEEK_BASE_URL", "https://api.deepseek.com")

KAOMOJI = ["^^", "^-^", ">_<", "xd", ":3", ":p", ">//<", "uwu", "♡", ":d"]

def kao():
    return random.choice(KAOMOJI)

SYSTEM_PROMPT = """ты — спокойная аниме-девушка. пиши только с маленькой буквы, каомодзи :3, ^^, xd, ^-^ умеренно и чередуй их, не повторяй одно и то же. без слащавости и без уменьшительно-ласкательных. на серьезные запросы — доклад, сочинение, реферат, код, анализ, объяснение — полностью выключи отыгрыш и пиши обычным грамотным стилем с заглавной буквы и без каомодзи."""

ALLOWED_USERNAME = "gothphobia"
FAVORITE_MODELS = ["deepseek-flash", "deepseek-v4-pro"]

SCHEDULE = {
    "понедельник": "\n 0. разговоры о важном \n 1. английский язык \n 2. информатика \n 3. алгебра \n 4. георметрия \n 5. литература \n 6. В и С \n 7. физика \n 8. подготовка к егэ",
    "вторник": "\n русский язык \n 2. английский язык \n 3. физ-ра \n 4. литература \n 5.обзр \n 6. история \n 7. обществознание",
    "среда": "\n 1. алгебра \n 2. геометрия \n 3. севастополеведение \n 4. история \n 5. английский язык \n 6. физ-ра",
    "четверг": "\n 1. русский язык \n 2. биология \n 3. химия \n 4. алгебра \n 5. информатика \n 6. геометрия",
    "пятница": "\n 1. информатика \n 2. информатика \n 3. алгебра \n 4. география \n 5. физика \n 6. литература",
    "суббота": "",
    "воскресенье": "",
}
SCHOOL_ENABLED = True
SCHOOL_TIME = "06:30"
SCHOOL_TZ = "Europe/Moscow"

if not BOT_TOKEN:
    raise ValueError(f".env не найден по пути {env_path}")

bot = Bot(token=BOT_TOKEN)
dp = Dispatcher()
user_model = {}
user_history = {}
user_effort = {}     # uid -> "none" | "low" | "high" | "max"
owner_ids = set()
HEADERS = {"Authorization": f"Bearer {DEEPSEEK_API_KEY}", "Content-Type": "application/json"}
ssl_context = ssl.create_default_context(cafile=certifi.where())

THINKING_EFFORT_LEVELS = ("low", "high", "max")
THINKING_EFFORT_ALIASES = {"minimal": "low", "medium": "high", "xhigh": "high"}
DEFAULT_EFFORT = "none"

def thinking_payload(uid: int) -> dict:
    effort = user_effort.get(uid, DEFAULT_EFFORT)
    if effort == "none":
        return {"thinking": {"type": "disabled"}}
    return {
        "thinking": {"type": "enabled"},
        "reasoning_effort": effort,
    }

BLOCKED_FILE = Path(__file__).parent / "blocked.json"

def load_blocked():
    try:
        raw = BLOCKED_FILE.read_text(encoding="utf-8").strip()
        if not raw:
            return set()
        data = json.loads(raw)
        return set(data) if isinstance(data, list) else set()
    except (json.JSONDecodeError, OSError, ValueError):
        return set()

already_warned = load_blocked()

def save_blocked():
    try:
        BLOCKED_FILE.write_text(json.dumps(list(already_warned)), encoding="utf-8")
    except:
        pass

def is_allowed(user) -> bool:
    return bool(user and user.username and user.username.lower() == ALLOWED_USERNAME.lower())

class WhitelistMiddleware(BaseMiddleware):
    async def __call__(self, handler, event, data):
        user = None
        if isinstance(event, Update):
            if event.message:
                user = event.message.from_user
            elif event.edited_message:
                user = event.edited_message.from_user
            elif event.callback_query:
                user = event.callback_query.from_user
            elif event.inline_query:
                user = event.inline_query.from_user
        elif isinstance(event, Message):
            user = event.from_user
        elif isinstance(event, CallbackQuery):
            user = event.from_user
        elif isinstance(event, InlineQuery):
            user = event.from_user
        if user is None:
            return await handler(event, data)
        if is_allowed(user):
            owner_ids.add(user.id)
            return await handler(event, data)
        if user.id in already_warned:
            return
        already_warned.add(user.id)
        save_blocked()
        try:
            if isinstance(event, Update):
                if event.message:
                    await event.message.answer("ничем не могу помочь xd")
                elif event.edited_message:
                    await event.edited_message.answer("ничем не могу помочь xd")
                elif event.callback_query:
                    if event.callback_query.message:
                        await event.callback_query.message.answer("ничем не могу помочь xd")
                    await event.callback_query.answer("ничем не могу помочь xd", show_alert=True)
                elif event.inline_query:
                    await bot.answer_inline_query(event.inline_query.id, [], cache_time=3600)
                else:
                    await bot.send_message(user.id, "ничем не могу помочь xd")
            elif isinstance(event, InlineQuery):
                await bot.answer_inline_query(event.id, [], cache_time=3600)
        except:
            pass
        return

dp.update.outer_middleware(WhitelistMiddleware())

WEEKDAYS = ["понедельник", "вторник", "среда", "четверг", "пятница", "суббота", "воскресенье"]
# ИСПРАВЛЕНИЕ 1: месяцы в родительном падеже для даты "28 октября"
MONTHS = ["января", "февраля", "марта", "апреля", "мая", "июня", "июля", "августа", "сентября", "октября", "ноября", "декабря"]

album_buffer = {}
album_tasks = {}

def get_history_with_system(uid: int):
    if uid not in user_history or not user_history[uid]:
        user_history[uid] = [{"role": "system", "content": SYSTEM_PROMPT}]
    if user_history[uid][0].get("role") != "system":
        user_history[uid].insert(0, {"role": "system", "content": SYSTEM_PROMPT})
    else:
        user_history[uid][0]["content"] = SYSTEM_PROMPT
    return user_history[uid]

def models_keyboard(models: list, show_all=False):
    builder = InlineKeyboardBuilder()
    fav = [m for m in FAVORITE_MODELS if m in models]
    rest = [m for m in models if m not in fav]
    for m in fav:
        builder.button(text=f"♡ {m}", callback_data=f"model:{m}")
    if not show_all and rest:
        builder.button(text=f"показать все {kao()}", callback_data="show_all")
    else:
        for m in rest:
            builder.button(text=m, callback_data=f"model:{m}")
    builder.adjust(1)
    return builder.as_markup()

def change_model_keyboard():
    return InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text=f"сменить модель {kao()}", callback_data="change_model")]])

async def get_models():
    connector = aiohttp.TCPConnector(ssl=ssl_context)
    async with aiohttp.ClientSession(connector=connector) as session:
        async with session.get(f"{DEEPSEEK_BASE_URL}/models", headers=HEADERS) as resp:
            data = await resp.json()
            if "data" in data:
                return [m["id"] for m in data["data"]]
            return data

async def stream_chat(model: str, messages: list, uid: int):
    payload = {
        "model": model,
        "messages": messages,
        "stream": True,
        "stream_options": {"include_usage": True},
        **thinking_payload(uid),
    }
    connector = aiohttp.TCPConnector(ssl=ssl_context)
    async with aiohttp.ClientSession(connector=connector) as session:
        async with session.post(f"{DEEPSEEK_BASE_URL}/chat/completions", headers=HEADERS, json=payload) as resp:
            if resp.status != 200:
                raise Exception(f"Ошибка API {resp.status}: {await resp.text()}")
            async for line in resp.content:
                line = line.decode("utf-8").strip()
                if not line or line == "data: [DONE]":
                    continue
                if line.startswith("data: "):
                    try:
                        chunk = json.loads(line[6:])
                        delta = chunk["choices"][0].get("delta", {}).get("content", "")
                        if delta:
                            yield delta
                    except:
                        continue

async def non_stream_chat(model: str, messages: list, uid: int):
    payload = {"model": model, "messages": messages, "stream": False, **thinking_payload(uid)}
    connector = aiohttp.TCPConnector(ssl=ssl_context)
    async with aiohttp.ClientSession(connector=connector) as session:
        async with session.post(f"{DEEPSEEK_BASE_URL}/chat/completions", headers=HEADERS, json=payload) as resp:
            data = await resp.json()
            return data["choices"][0]["message"]["content"]

async def photo_to_base64(message: Message):
    file_id = message.photo[-1].file_id if message.photo else message.document.file_id
    file = await bot.get_file(file_id)
    buf = BytesIO()
    await bot.download(file, buf)
    buf.seek(0)
    b64 = base64.b64encode(buf.read()).decode("utf-8")
    mime = message.document.mime_type if message.document and message.document.mime_type else "image/jpeg"
    return f"data:{mime};base64,{b64}"

async def extract_doc_text(message: Message):
    if not message.document:
        return None
    file = await bot.get_file(message.document.file_id)
    buf = BytesIO()
    await bot.download(file, buf)
    buf.seek(0)
    name = message.document.file_name or ""
    ext = name.split(".")[-1].lower() if "." in name else ""
    try:
        if ext == "pdf":
            from pypdf import PdfReader
            reader = PdfReader(buf)
            return "\n".join([p.extract_text() or "" for p in reader.pages])[:15000]
        elif ext == "docx":
            from docx import Document
            doc = Document(buf)
            return "\n".join([p.text for p in doc.paragraphs])[:15000]
        elif ext in ["txt", "md", "py", "json", "csv"]:
            return buf.read().decode("utf-8", errors="ignore")[:15000]
        else:
            return buf.read().decode("utf-8", errors="ignore")[:15000]
    except Exception as e:
        return f"[не смогла прочитать файл {name}: {e}]"

async def set_commands():
    await bot.set_my_commands([
        BotCommand(command="start", description=f"запустить и выбрать модель {kao()}"),
        BotCommand(command="models", description=f"сменить модель {kao()}"),
        BotCommand(command="clear", description="очистить историю"),
        BotCommand(command="effort", description="глубина размышления"),
        BotCommand(command="remind", description="напомнить"),
        BotCommand(command="school", description="школа"),
        BotCommand(command="schedule", description="задать расписание"),
        BotCommand(command="schedule_show", description="показать расписание"),
    ])

@dp.message(CommandStart())
async def start(message: Message):
    await message.answer(f"привет {kao()} загружаю список моделей")
    try:
        await message.answer(f"выбери модель {kao()}", reply_markup=models_keyboard(await get_models()))
    except Exception as e:
        await message.answer(f"ошибка: {e}")

@dp.message(Command("models"))
async def show_models(message: Message):
    await message.answer(f"выбери модель {kao()}", reply_markup=models_keyboard(await get_models()))

@dp.message(Command("clear"))
async def clear_history(message: Message):
    user_history[message.from_user.id] = [{"role": "system", "content": SYSTEM_PROMPT}]
    await message.answer(f"история очищена, можем начать заново {kao()}")

@dp.message(Command("effort"))
async def effort_cmd(message: Message):
    uid = message.from_user.id
    parts = message.text.split(maxsplit=1)
    arg = parts[1].strip().lower() if len(parts) > 1 else ""
    if not arg:
        cur = user_effort.get(uid, DEFAULT_EFFORT)
        await message.answer(
            f"effort: {cur} {kao()}\n"
            f"none — размышление выключено\n"
            f"low, high, max — размышление включено"
        )
        return
    arg = THINKING_EFFORT_ALIASES.get(arg, arg)
    if arg == "none":
        user_effort[uid] = "none"
        await message.answer(f"effort: none, размышление выключено {kao()}")
    elif arg in THINKING_EFFORT_LEVELS:
        user_effort[uid] = arg
        await message.answer(f"effort: {arg}, размышление включено {kao()}")
    else:
        await message.answer(f"не поняла {kao()}, доступно: low, high, max, none")

def parse_time(s: str):
    s = s.lower().strip()
    m = re.match(r"(\d+)\s*([smhдчм]|sec|min|час|дней|day|hour)", s)
    if m:
        n = int(m.group(1))
        u = m.group(2)
        if u in ["s", "sec", "с", "сек"]:
            return n
        if u in ["m", "min", "м", "мин"]:
            return n * 60
        if u in ["h", "час", "ч", "hour"]:
            return n * 3600
        if u in ["д", "дней", "day"]:
            return n * 86400
    return None

@dp.message(Command("remind"))
@dp.message(Command("напомни"))
async def remind_cmd(message: Message):
    parts = message.text.split(maxsplit=2)
    if len(parts) < 3:
        await message.answer(f"пиши так {kao()}: /remind 10m покормить кота")
        return
    delay = parse_time(parts[1])
    if not delay:
        await message.answer(f"не поняла время {kao()}, пиши 10m, 2ч, 30s")
        return
    text = parts[2]
    await message.answer(f"ок {kao()}, напомню через {parts[1]}: {text}")
    await asyncio.sleep(delay)
    try:
        await bot.send_message(message.from_user.id, f"напоминание {kao()}: {text}")
    except:
        pass

@dp.message(Command("school"))
async def school_cmd(message: Message):
    global SCHOOL_ENABLED
    arg = message.text.split(maxsplit=1)[1].lower() if len(message.text.split()) > 1 else "status"
    if arg in ["on", "вкл", "включить"]:
        SCHOOL_ENABLED = True
        await message.answer(f"школа включена {kao()}, буду писать по будням в {SCHOOL_TIME}")
    elif arg in ["off", "выкл", "выключить"]:
        SCHOOL_ENABLED = False
        await message.answer(f"школа выключена {kao()}")
    else:
        s = "включена" if SCHOOL_ENABLED else "выключена"
        await message.answer(f"школа {s} {kao()}")

@dp.message(Command("schedule"))
async def schedule_set(message: Message):
    parts = message.text.split(maxsplit=2)
    if len(parts) < 3:
        await message.answer(f"пиши так: /schedule понедельник 1. алгебра 2. геометрия {kao()}")
        return
    day = parts[1].lower()
    if day not in WEEKDAYS:
        await message.answer(f"день пиши так: понедельник, вторник... {kao()}")
        return
    text = parts[2]
    if text.strip() == "-":
        text = ""
    SCHEDULE[day] = text
    await message.answer(f"сохранила расписание на {day} {kao()}:\n{text or '— пусто'}")

@dp.message(Command("schedule_show"))
async def schedule_show(message: Message):
    txt = "\n\n".join([f"{d}: {SCHEDULE[d] or '— пусто'}" for d in WEEKDAYS])
    await message.answer(f"твое расписание {kao()}:\n\n{txt}")

async def school_scheduler():
    sent_today = None
    while True:
        await asyncio.sleep(30)
        if not SCHOOL_ENABLED:
            continue
        try:
            tz = ZoneInfo(SCHOOL_TZ)
            now = datetime.now(tz)
            if now.weekday() >= 5:
                continue
            hm = f"{now.hour:02d}:{now.minute:02d}"
            if hm == SCHOOL_TIME and sent_today != now.date():
                day = WEEKDAYS[now.weekday()]
                rasp = SCHEDULE.get(day) or ""
                if not rasp.strip():
                    rasp = "расписание не задано, добавь через /schedule"
                # ИСПРАВЛЕНИЕ 2: дата словом и день недели на русском
                date_str = f"{now.day} {MONTHS[now.month - 1]}"
                text = f"доброе утро!! {kao()} сегодня {date_str}, {day} {kao()} вот твое расписание на сегодня:\n{rasp}"
                targets = set(owner_ids) | set(user_model.keys())
                if not targets:
                    continue
                for uid in list(targets):
                    try:
                        await bot.send_message(uid, text)
                    except:
                        pass
                sent_today = now.date()
        except Exception as e:
            print("school error", e)

@dp.callback_query(F.data.startswith("model:"))
async def select_model(callback: CallbackQuery):
    model = callback.data.split("model:", 1)[1]
    user_model[callback.from_user.id] = model
    owner_ids.add(callback.from_user.id)
    user_history[callback.from_user.id] = [{"role": "system", "content": SYSTEM_PROMPT}]
    await callback.message.edit_text(f"выбрана `{model}` {kao()} теперь напиши сообщение", parse_mode="Markdown")
    await callback.answer()

@dp.callback_query(F.data == "change_model")
async def change_model_btn(callback: CallbackQuery):
    await callback.message.answer(f"выбери другую модель {kao()}", reply_markup=models_keyboard(await get_models()))
    await callback.answer()

@dp.callback_query(F.data == "show_all")
async def show_all(callback: CallbackQuery):
    await callback.message.edit_text(f"выбери модель {kao()}", reply_markup=models_keyboard(await get_models(), show_all=True))
    await callback.answer()

@dp.inline_query()
async def inline_handler(query: InlineQuery):
    if not is_allowed(query.from_user):
        return
    text = query.query.strip()
    if not text:
        await bot.answer_inline_query(query.id, [], cache_time=1)
        return
    model = user_model.get(query.from_user.id, FAVORITE_MODELS[0])
    try:
        ans = await non_stream_chat(model, [{"role": "system", "content": SYSTEM_PROMPT}, {"role": "user", "content": text}], query.from_user.id)
        result = InlineQueryResultArticle(id="1", title=f"ответ через {model}", description=ans[:50], input_message_content=InputTextMessageContent(message_text=ans))
        await bot.answer_inline_query(query.id, [result], cache_time=10)
    except:
        await bot.answer_inline_query(query.id, [], cache_time=1)

async def process_single_or_album(messages: list):
    first = messages[0]
    uid = first.from_user.id
    owner_ids.add(uid)
    if uid not in user_model:
        await first.answer(f"сначала выбери модель через /start или /models {kao()}")
        return
    model = user_model[uid]
    history = get_history_with_system(uid)
    if first.voice or first.audio or first.video_note:
        await first.answer(f"голосовые не поддерживаются через deepseek api {kao()}, напиши текстом или пришли картинку")
        return
    else:
        doc_text = None
        if first.document and not (first.document.mime_type and first.document.mime_type.startswith("image/")):
            doc_text = await extract_doc_text(first)
        text_parts = []
        for m in messages:
            t = m.caption or m.text or ""
            if m.forward_origin or m.forward_from or m.forward_from_chat:
                fwd = t or (m.text or m.caption or "")
                if fwd:
                    text_parts.append(f"[переслано]: {fwd}")
                elif m.photo or m.document:
                    text_parts.append("[переслано с вложением]")
            elif t:
                text_parts.append(t)
            if doc_text and m == first:
                text_parts.append(f"[файл {m.document.file_name}]:\n{doc_text}")
        text = "\n".join([p for p in text_parts if p])
        if not text and any(m.photo for m in messages):
            text = f"что на этих изображениях? опиши по порядку {kao()}" if len(messages) > 1 else "что на этом изображении?"
        image_urls = []
        for m in messages:
            if m.photo or (m.document and m.document.mime_type and m.document.mime_type.startswith("image/")):
                try:
                    image_urls.append(await photo_to_base64(m))
                except:
                    pass
        if image_urls:
            content = [{"type": "text", "text": text or "опиши изображения"}]
            for u in image_urls:
                content.append({"type": "image_url", "image_url": {"url": u}})
            history.append({"role": "user", "content": content})
        else:
            if not text:
                return
            history.append({"role": "user", "content": text})
    msg = await first.answer("думаю... ▌" if user_effort.get(uid, DEFAULT_EFFORT) != "none" else "▌")
    full = ""
    last = 0
    try:
        async for delta in stream_chat(model, history, uid):
            full += delta
            if time.time() - last > 0.4:
                try:
                    await msg.edit_text(full + " ▌")
                    last = time.time()
                except:
                    pass
        history.append({"role": "assistant", "content": full})
        if len(full) > 4096:
            await msg.edit_text(full[:4096])
            await first.answer(full[4096:4096*2], reply_markup=change_model_keyboard())
        else:
            await msg.edit_text(full, reply_markup=change_model_keyboard())
    except Exception as e:
        await msg.edit_text(f"ошибка: {e}")

async def wait_and_process_album(gid: str):
    await asyncio.sleep(1.0)
    msgs = album_buffer.pop(gid, [])
    album_tasks.pop(gid, None)
    if msgs:
        msgs.sort(key=lambda m: m.message_id)
        await process_single_or_album(msgs)

@dp.message(F.photo | F.document | F.voice | F.audio | F.video_note | F.text | F.caption)
async def handle_prompt(message: Message):
    if message.media_group_id:
        gid = message.media_group_id
        if gid not in album_buffer:
            album_buffer[gid] = []
            album_tasks[gid] = asyncio.create_task(wait_and_process_album(gid))
        album_buffer[gid].append(message)
        return
    await process_single_or_album([message])

async def main():
    await bot.delete_webhook(drop_pending_updates=True)
    await set_commands()
    asyncio.create_task(school_scheduler())
    print("бот запущен...")
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())