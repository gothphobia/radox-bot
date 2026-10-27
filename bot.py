import os
import json
import asyncio
import time
import random
import ssl
import base64
import tempfile
import certifi
import aiohttp
from io import BytesIO
from pathlib import Path
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
from dotenv import load_dotenv
from apscheduler.triggers.cron import CronTrigger

from aiogram import Bot, Dispatcher, F
from aiogram.types import Message, CallbackQuery, BotCommand, InlineKeyboardButton, InlineKeyboardMarkup, ReplyKeyboardMarkup, KeyboardButton, Update, InlineQuery, InlineQueryResultArticle, InputTextMessageContent, FSInputFile
from aiogram.filters import CommandStart, Command
from aiogram.utils.keyboard import InlineKeyboardBuilder
from aiogram.dispatcher.middlewares.base import BaseMiddleware
from aiogram.exceptions import TelegramBadRequest

import briefing
import db
import memory
import scheduler as sched
import voice
from tools import TOOL_SCHEMAS, ToolContext, execute_tool

env_path = Path(__file__).parent / ".env"
load_dotenv(dotenv_path=env_path)

BOT_TOKEN = os.getenv("BOT_TOKEN")
DEEPSEEK_API_KEY = os.getenv("DEEPSEEK_API_KEY")
DEEPSEEK_BASE_URL = os.getenv("DEEPSEEK_BASE_URL", "https://api.deepseek.com")

KAOMOJI = ["^^", "^-^", ">_<", "xd", ":3", ":p", ">//<", "uwu", "♡", ":d"]

def kao():
    return random.choice(KAOMOJI)

SYSTEM_PROMPT = """ты — спокойная аниме-девушка. пиши только с маленькой буквы, каомодзи :3, ^^, xd, ^-^ умеренно и чередуй их, не повторяй одно и то же. без слащавости и без уменьшительно-ласкательных. на серьезные запросы — доклад, сочинение, реферат, код, анализ, объяснение — полностью выключи отыгрыш и пиши обычным грамотным стилем с заглавной буквы и без каомодзи."""

TOOL_PROMPT = """\n\nу тебя есть инструменты: напоминания, список дел, календарь, погода, школьное расписание, текущее время, интернет-поиск, чтение страниц и долговременная память. используй их, когда просят что-то сделать, а не только отвечай текстом. не выдумывай расписание, погоду, события и напоминания — бери данные только из инструментов. если нужны свежие данные или факты из интернета — используй web_search, при необходимости открой страницу через fetch_page. если использовала web_search — обязательно приведи ссылки на источники в ответе. если инструмент вернул ошибку, коротко скажи, что не получилось. когда просят запомнить что-то важное — вызывай remember; не придумывай факты о пользователе, которых нет в памяти."""

ALLOWED_USERNAME = "gothphobia"
FAVORITE_MODELS = ["deepseek-flash", "deepseek-v4-pro"]

DEFAULT_SCHEDULE = {
    "понедельник": "\n 0. разговоры о важном \n 1. английский язык \n 2. информатика \n 3. алгебра \n 4. георметрия \n 5. литература \n 6. В и С \n 7. физика \n 8. подготовка к егэ",
    "вторник": "\n 1. русский язык \n 2. английский язык \n 3. физ-ра \n 4. литература \n 5. обзр \n 6. история \n 7. обществознание",
    "среда": "\n 1. алгебра \n 2. геометрия \n 3. севастополеведение \n 4. история \n 5. английский язык \n 6. физ-ра",
    "четверг": "\n 1. русский язык \n 2. биология \n 3. химия \n 4. алгебра \n 5. информатика \n 6. геометрия",
    "пятница": "\n 1. информатика \n 2. информатика \n 3. алгебра \n 4. география \n 5. физика \n 6. литература",
    "суббота": "",
    "воскресенье": "",
}
BRIEFING_WEEKDAY_TIME = os.getenv("BRIEFING_WEEKDAY_TIME", "06:30")
BRIEFING_WEEKEND_TIME = os.getenv("BRIEFING_WEEKEND_TIME", "09:00")
SCHOOL_TZ = "Europe/Moscow"
MAX_TOOL_ROUNDS = 5

if not BOT_TOKEN:
    raise ValueError(f".env не найден по пути {env_path}")

bot = Bot(token=BOT_TOKEN)
dp = Dispatcher()
background_tasks = set()
pending: dict[int, tuple[str, str | None]] = {}  # ожидание ввода: город, расписание

MAIN_MENU = ReplyKeyboardMarkup(
    keyboard=[
        [KeyboardButton(text="📅 Календарь"), KeyboardButton(text="✅ Дела")],
        [KeyboardButton(text="⏰ Напоминания"), KeyboardButton(text="🧠 Память")],
        [KeyboardButton(text="🌅 Брифинг"), KeyboardButton(text="⚙️ Настройки")],
        [KeyboardButton(text="❓ Помощь")],
    ],
    resize_keyboard=True,
    is_persistent=True,
    input_field_placeholder="напиши сообщение или выбери раздел",
)

HELP_TEXT = """просто напиши, что нужно — я пойму:

• напомни завтра в 9 про математику
• поставь встречу в четверг в 16:00
• что у меня сегодня по расписанию?
• какая погода завтра?
• найди в интернете …
• запомни, что я люблю зелёный чай
• ответь голосом: …

кнопки внизу открывают календарь, дела, напоминания, память, брифинг и настройки."""


def spawn(coro):
    """запустить фоновую задачу и не потерять ссылку на неё."""
    task = asyncio.create_task(coro)
    background_tasks.add(task)

    def done(t):
        background_tasks.discard(t)
        if not t.cancelled() and t.exception():
            print("background error", t.exception())

    task.add_done_callback(done)
    return task


HEADERS = {"Authorization": f"Bearer {DEEPSEEK_API_KEY}", "Content-Type": "application/json"}
ssl_context = ssl.create_default_context(cafile=certifi.where())

THINKING_EFFORT_LEVELS = ("low", "high", "max")
THINKING_EFFORT_ALIASES = {"minimal": "low", "medium": "high", "xhigh": "high"}
DEFAULT_EFFORT = "none"

def thinking_payload(uid: int) -> dict:
    effort = db.get_user(uid)["effort"] or DEFAULT_EFFORT
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

def build_system_prompt(uid: int, include_tools: bool | None = None) -> str:
    user = db.get_user(uid)
    now = datetime.now(ZoneInfo(SCHOOL_TZ))
    prompt = (
        SYSTEM_PROMPT
        + f"\n\n[сейчас {now.day} {MONTHS[now.month - 1]} {now.year}, "
          f"{WEEKDAYS[now.weekday()]}, {now.hour:02d}:{now.minute:02d}, "
          f"часовой пояс {SCHOOL_TZ}, город {user['city']}]"
    )
    if include_tools is None:
        include_tools = bool(user["tools"])
    if include_tools:
        prompt += TOOL_PROMPT
    facts = memory.facts_for_prompt(uid)
    summary = memory.summary_for_prompt(uid)
    if facts:
        prompt += f"\n\n[что я помню о пользователе]\n{facts}"
    if summary:
        prompt += f"\n\n[конспект прошлых разговоров]\n{summary}"
    return prompt


def content_for_storage(content):
    """картинки в историю не тащим — только текстовую пометку."""
    if isinstance(content, str):
        return content
    texts, notes = [], []
    for part in content:
        if part.get("type") == "text":
            texts.append(part.get("text", ""))
        elif part.get("type") == "image_url":
            notes.append("[изображение]")
    text = "\n".join(t for t in texts if t)
    if notes:
        text = f"{text}\n{' '.join(notes)}".strip()
    return text

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

async def stream_chat(model: str, messages: list, uid: int, tools: list | None = None):
    """отдаёт события: ("content", текст) по мере генерации и в конце ("tool_calls", [...])."""
    payload = {
        "model": model,
        "messages": messages,
        "stream": True,
        "stream_options": {"include_usage": True},
        **thinking_payload(uid),
    }
    if tools:
        payload["tools"] = tools
        payload["tool_choice"] = "auto"
    connector = aiohttp.TCPConnector(ssl=ssl_context)
    tool_calls: dict[int, dict] = {}
    async with aiohttp.ClientSession(connector=connector) as session:
        async with session.post(f"{DEEPSEEK_BASE_URL}/chat/completions", headers=HEADERS, json=payload) as resp:
            if resp.status != 200:
                raise Exception(f"Ошибка API {resp.status}: {await resp.text()}")
            async for line in resp.content:
                line = line.decode("utf-8").strip()
                if not line or line == "data: [DONE]":
                    continue
                if not line.startswith("data: "):
                    continue
                try:
                    chunk = json.loads(line[6:])
                except json.JSONDecodeError:
                    continue
                choices = chunk.get("choices") or []
                if not choices:
                    continue
                delta = choices[0].get("delta") or {}
                content = delta.get("content")
                if content:
                    yield ("content", content)
                for tc in delta.get("tool_calls") or []:
                    idx = tc.get("index", 0)
                    acc = tool_calls.setdefault(
                        idx,
                        {"id": "", "type": "function", "function": {"name": "", "arguments": ""}},
                    )
                    if tc.get("id"):
                        acc["id"] = tc["id"]
                    fn = tc.get("function") or {}
                    if fn.get("name"):
                        acc["function"]["name"] += fn["name"]
                    if fn.get("arguments"):
                        acc["function"]["arguments"] += fn["arguments"]
    if tool_calls:
        yield ("tool_calls", [tool_calls[i] for i in sorted(tool_calls)])

async def non_stream_chat(model: str, messages: list, uid: int):
    payload = {"model": model, "messages": messages, "stream": False, **thinking_payload(uid)}
    connector = aiohttp.TCPConnector(ssl=ssl_context)
    async with aiohttp.ClientSession(connector=connector) as session:
        async with session.post(f"{DEEPSEEK_BASE_URL}/chat/completions", headers=HEADERS, json=payload) as resp:
            data = await resp.json()
            return data["choices"][0]["message"]["content"]


async def run_agent(model: str, messages: list, uid: int, msg: Message, use_tools: bool) -> tuple[str, bool]:
    """цикл «модель -> инструменты -> модель». возвращает (текст, просили ли голос)."""
    user = db.get_user(uid)
    ctx = ToolContext(uid=uid, tz=ZoneInfo(SCHOOL_TZ), city=user["city"])
    schemas = TOOL_SCHEMAS if use_tools else None
    full = ""
    for _ in range(MAX_TOOL_ROUNDS):
        full = ""
        tool_calls = None
        last_edit = 0.0
        async for kind, payload in stream_chat(model, messages, uid, schemas):
            if kind == "content":
                full += payload
                if time.time() - last_edit > 0.4:
                    try:
                        await msg.edit_text(full + " ▌")
                    except Exception:
                        pass
                    last_edit = time.time()
            elif kind == "tool_calls":
                tool_calls = payload
        if not tool_calls:
            return full, ctx.voice_requested

        messages.append({"role": "assistant", "content": full or None, "tool_calls": tool_calls})
        for call in tool_calls:
            name = call["function"]["name"]
            try:
                args = json.loads(call["function"]["arguments"] or "{}")
            except json.JSONDecodeError:
                args = None
            try:
                await msg.edit_text(f"⚙ {name}…")
            except Exception:
                pass
            if args is None:
                result = "ошибка: не удалось разобрать аргументы инструмента"
            else:
                result = await execute_tool(name, args, ctx)
            messages.append(
                {"role": "tool", "tool_call_id": call["id"], "content": result[:4000]}
            )
    return full or "слишком много шагов подряд, остановилась", ctx.voice_requested


def voice_file_info(message: Message):
    if message.voice:
        return message.voice.file_id, ".ogg"
    if message.audio:
        suffix = Path(message.audio.file_name or "").suffix or ".mp3"
        return message.audio.file_id, suffix
    if message.video_note:
        return message.video_note.file_id, ".mp4"
    return None, None


async def transcribe_message(message: Message, status: Message) -> str | None:
    file_id, suffix = voice_file_info(message)
    if not file_id:
        return None
    file = await bot.get_file(file_id)
    with tempfile.TemporaryDirectory() as td:
        path = Path(td) / f"input{suffix}"
        await bot.download(file, path)
        try:
            await status.edit_text("слушаю…")
        except Exception:
            pass
        return await voice.transcribe(path)


async def send_voice_reply(message: Message, text: str):
    with tempfile.TemporaryDirectory() as td:
        try:
            ogg = await voice.synthesize(text, td)
        except Exception as e:
            await message.answer(f"не получилось озвучить: {e}")
            return
        if ogg is None:
            return
        await message.answer_voice(FSInputFile(ogg))


async def send_voice_to(uid: int, text: str):
    with tempfile.TemporaryDirectory() as td:
        ogg = await voice.synthesize(text, td)
        if ogg is not None:
            await bot.send_voice(uid, FSInputFile(ogg))

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
        BotCommand(command="start", description="главное меню"),
    ])

@dp.message(CommandStart())
async def start(message: Message):
    uid = message.from_user.id
    pending.pop(uid, None)
    await message.answer(
        "привет ^^ я на связи.\nвыбирай раздел кнопками внизу или просто напиши, что нужно.",
        reply_markup=MAIN_MENU,
    )
    user = db.get_user(uid)
    if not user["model"]:
        try:
            await message.answer(f"для начала выбери модель {kao()}", reply_markup=models_keyboard(await get_models()))
        except Exception as e:
            await message.answer(f"ошибка: {e}")

@dp.message(Command("models"))
async def show_models(message: Message):
    await message.answer(f"выбери модель {kao()}", reply_markup=models_keyboard(await get_models()))

@dp.message(Command("clear"))
async def clear_history(message: Message):
    db.clear_history(message.from_user.id)
    await message.answer(f"история очищена, можем начать заново {kao()}")

@dp.message(Command("effort"))
async def effort_cmd(message: Message):
    uid = message.from_user.id
    parts = message.text.split(maxsplit=1)
    arg = parts[1].strip().lower() if len(parts) > 1 else ""
    if not arg:
        cur = db.get_user(uid)["effort"] or DEFAULT_EFFORT
        await message.answer(
            f"effort: {cur} {kao()}\n"
            f"none — размышление выключено\n"
            f"low, high, max — размышление включено"
        )
        return
    arg = THINKING_EFFORT_ALIASES.get(arg, arg)
    if arg == "none":
        db.set_user(uid, effort="none")
        await message.answer(f"effort: none, размышление выключено {kao()}")
    elif arg in THINKING_EFFORT_LEVELS:
        db.set_user(uid, effort=arg)
        await message.answer(f"effort: {arg}, размышление включено {kao()}")
    else:
        await message.answer(f"не поняла {kao()}, доступно: low, high, max, none")

@dp.message(Command("reminders"))
async def reminders_cmd(message: Message):
    rows = db.list_reminders(message.from_user.id)
    if not rows:
        await message.answer(f"активных напоминаний нет {kao()}")
        return
    tz = ZoneInfo(SCHOOL_TZ)
    lines = []
    for r in rows:
        due = datetime.fromtimestamp(r["due_ts"], tz)
        repeat = {"daily": " (каждый день)", "weekly": " (каждую неделю)"}.get(r["repeat"], "")
        lines.append(f"#{r['id']} — {due:%d.%m %H:%M}{repeat}: {r['text']}")
    await message.answer(f"напоминания {kao()}:\n" + "\n".join(lines))


@dp.message(Command("todos"))
async def todos_cmd(message: Message):
    rows = db.list_todos(message.from_user.id, include_done=True)
    if not rows:
        await message.answer(f"список дел пуст {kao()}")
        return
    lines = [f"{'✓' if t['done'] else '•'} #{t['id']} — {t['text']}" for t in rows]
    await message.answer(f"дела {kao()}:\n" + "\n".join(lines))


@dp.message(Command("events"))
async def events_cmd(message: Message):
    uid = message.from_user.id
    parts = message.text.split(maxsplit=1)
    try:
        days = int(parts[1]) if len(parts) > 1 else 7
    except (ValueError, IndexError):
        days = 7
    days = max(1, min(days, 365))
    tz = ZoneInfo(SCHOOL_TZ)
    now = datetime.now(tz)
    rows = db.list_events(uid, int(now.timestamp()), int((now + timedelta(days=days)).timestamp()))
    if not rows:
        await message.answer(f"событий нет на {days} дн. вперёд {kao()}")
        return
    lines = []
    for e in rows:
        start = datetime.fromtimestamp(e["starts_ts"], tz)
        end = datetime.fromtimestamp(e["ends_ts"], tz)
        span = (
            f"{start:%d.%m %H:%M}–{end:%H:%M}"
            if start.date() == end.date()
            else f"{start:%d.%m %H:%M} – {end:%d.%m %H:%M}"
        )
        note = f" — {e['notes']}" if e["notes"] else ""
        lines.append(f"#{e['id']} — {span} — {e['title']}{note}")
    await message.answer(f"календарь на {days} дн. {kao()}:\n" + "\n".join(lines))


@dp.message(Command("city"))
async def city_cmd(message: Message):
    uid = message.from_user.id
    parts = message.text.split(maxsplit=1)
    if len(parts) < 2 or not parts[1].strip():
        await message.answer(f"город для погоды: {db.get_user(uid)['city']} {kao()}\nпиши: /city Москва")
        return
    city = parts[1].strip()[:80]
    db.set_user(uid, city=city)
    await message.answer(f"город сохранен: {city} {kao()}")


@dp.message(Command("tools"))
async def tools_cmd(message: Message):
    uid = message.from_user.id
    parts = message.text.split(maxsplit=1)
    arg = parts[1].strip().lower() if len(parts) > 1 else ""
    if arg in ("on", "вкл", "включить"):
        db.set_user(uid, tools=1)
        await message.answer(f"инструменты включены {kao()}")
    elif arg in ("off", "выкл", "выключить"):
        db.set_user(uid, tools=0)
        await message.answer(f"инструменты выключены {kao()}")
    else:
        state = "включены" if db.get_user(uid)["tools"] else "выключены"
        await message.answer(f"инструменты {state} {kao()}, пиши /tools on или /tools off")


@dp.message(Command("voice"))
async def voice_cmd(message: Message):
    uid = message.from_user.id
    parts = message.text.split(maxsplit=1)
    arg = parts[1].strip().lower() if len(parts) > 1 else ""
    if arg in ("on", "вкл", "включить"):
        db.set_user(uid, voice=1)
        await message.answer(f"голосовые ответы включены {kao()}, буду озвучивать все ответы")
    elif arg in ("off", "выкл", "выключить"):
        db.set_user(uid, voice=0)
        await message.answer(f"голосовые ответы выключены {kao()}, отвечаю текстом")
    else:
        state = "включены" if db.get_user(uid)["voice"] else "выключены"
        await message.answer(
            f"голосовые ответы {state} {kao()}\n"
            f"/voice on — озвучивать все ответы\n"
            f"/voice off — только текст\n"
            f"одно сообщение можно озвучить фразой «ответь голосом»"
        )


@dp.message(Command("say"))
async def say_cmd(message: Message):
    parts = message.text.split(maxsplit=1)
    if len(parts) < 2 or not parts[1].strip():
        await message.answer(f"пиши так {kao()}: /say текст, который нужно озвучить")
        return
    await send_voice_reply(message, parts[1])


@dp.message(Command("memory"))
async def memory_cmd(message: Message):
    uid = message.from_user.id
    facts = db.list_facts(uid)
    summary = db.get_summary(uid)
    if not facts and not summary:
        await message.answer(
            f"пока ничего не помню {kao()}\n"
            f"скажи «запомни, что...» или просто общайся — важное я сохраню сама"
        )
        return
    lines = [f"#{f['id']} — {f['text']}" for f in facts[:60]]
    if len(facts) > 60:
        lines.append(f"… и ещё {len(facts) - 60}")
    if summary:
        if lines:
            lines.append("")
        lines.append(f"конспект прошлых разговоров: {len(summary['text'])} символов")
    await message.answer(f"что я помню {kao()}:\n" + "\n".join(lines))


@dp.message(Command("remember"))
async def remember_cmd(message: Message):
    parts = message.text.split(maxsplit=1)
    if len(parts) < 2 or not parts[1].strip():
        await message.answer(f"пиши так {kao()}: /remember любит зелёный чай")
        return
    fid = db.add_fact(message.from_user.id, parts[1].strip())
    if fid is None:
        await message.answer(f"это я уже помню {kao()}")
    else:
        await message.answer(f"запомнила #{fid} {kao()}")


@dp.message(Command("forget"))
async def forget_cmd(message: Message):
    uid = message.from_user.id
    parts = message.text.split(maxsplit=1)
    arg = parts[1].strip().lower() if len(parts) > 1 else ""
    if arg in ("all", "все", "всё"):
        db.clear_facts(uid)
        await message.answer(f"все факты удалены {kao()}")
        return
    try:
        fid = int(arg)
    except ValueError:
        await message.answer(f"пиши так {kao()}: /forget 3 или /forget all")
        return
    if db.delete_fact(uid, fid):
        await message.answer(f"факт #{fid} удалён {kao()}")
    else:
        await message.answer(f"факт #{fid} не найден {kao()}")


@dp.message(Command("briefing"))
@dp.message(Command("school"))
async def briefing_cmd(message: Message):
    uid = message.from_user.id
    parts = message.text.split(maxsplit=1)
    arg = parts[1].strip().lower() if len(parts) > 1 else ""
    if arg in ("on", "вкл", "включить"):
        db.set_setting("school_enabled", "1")
        await message.answer(
            f"утренний брифинг включен {kao()}: будни в {BRIEFING_WEEKDAY_TIME}, "
            f"выходные в {BRIEFING_WEEKEND_TIME}"
        )
    elif arg in ("off", "выкл", "выключить"):
        db.set_setting("school_enabled", "0")
        await message.answer(f"утренний брифинг выключен {kao()}")
    elif arg == "test":
        await message.answer(f"собираю брифинг {kao()}...")
        await morning_briefing(target_uid=uid, force=True)
    else:
        state = "включен" if db.get_setting("school_enabled", "1") == "1" else "выключен"
        await message.answer(
            f"утренний брифинг {state} {kao()}\n"
            f"будни в {BRIEFING_WEEKDAY_TIME}, выходные в {BRIEFING_WEEKEND_TIME}\n"
            f"/briefing on|off, /briefing test — прислать сейчас"
        )

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
    db.set_schedule(day, text)
    await message.answer(f"сохранила расписание на {day} {kao()}:\n{text or '— пусто'}")

@dp.message(Command("schedule_show"))
async def schedule_show(message: Message):
    schedule = db.get_schedule()
    txt = "\n\n".join([f"{d}: {schedule.get(d) or '— пусто'}" for d in WEEKDAYS])
    await message.answer(f"твое расписание {kao()}:\n\n{txt}")

async def morning_briefing(target_uid: int | None = None, force: bool = False):
    if not force and db.get_setting("school_enabled", "1") != "1":
        return
    now = datetime.now(ZoneInfo(SCHOOL_TZ))
    uids = [target_uid] if target_uid is not None else db.all_user_ids()
    weather_cache = {}
    for uid in uids:
        try:
            text = await briefing.build(uid, now, weather_cache)
        except Exception as e:
            print("briefing build error", e)
            continue
        try:
            await bot.send_message(uid, text)
        except Exception as e:
            print("briefing send error", e)
            continue
        if db.get_user(uid)["voice"]:
            try:
                await send_voice_to(uid, text)
            except Exception as e:
                print("briefing voice error", e)

@dp.callback_query(F.data.startswith("model:"))
async def select_model(callback: CallbackQuery):
    model = callback.data.split("model:", 1)[1]
    uid = callback.from_user.id
    db.set_user(uid, model=model)
    db.clear_history(uid)
    markup = InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="⚙️ настройки", callback_data="set:home")
    ]])
    await callback.message.edit_text(
        f"выбрана `{model}` {kao()} теперь напиши сообщение",
        parse_mode="Markdown",
        reply_markup=markup,
    )
    await callback.answer()

@dp.callback_query(F.data == "change_model")
async def change_model_btn(callback: CallbackQuery):
    markup = models_keyboard(await get_models())
    markup.inline_keyboard.append([InlineKeyboardButton(text="⚙️ настройки", callback_data="set:home")])
    await callback.message.answer(f"выбери другую модель {kao()}", reply_markup=markup)
    await callback.answer()

@dp.callback_query(F.data == "show_all")
async def show_all(callback: CallbackQuery):
    markup = models_keyboard(await get_models(), show_all=True)
    markup.inline_keyboard.append([InlineKeyboardButton(text="⚙️ настройки", callback_data="set:home")])
    await callback.message.edit_text(f"выбери модель {kao()}", reply_markup=markup)
    await callback.answer()

@dp.inline_query()
async def inline_handler(query: InlineQuery):
    if not is_allowed(query.from_user):
        return
    text = query.query.strip()
    if not text:
        await bot.answer_inline_query(query.id, [], cache_time=1)
        return
    model = db.get_user(query.from_user.id)["model"] or FAVORITE_MODELS[0]
    try:
        ans = await non_stream_chat(model, [{"role": "system", "content": build_system_prompt(query.from_user.id, include_tools=False)}, {"role": "user", "content": text}], query.from_user.id)
        result = InlineQueryResultArticle(id="1", title=f"ответ через {model}", description=ans[:50], input_message_content=InputTextMessageContent(message_text=ans))
        await bot.answer_inline_query(query.id, [result], cache_time=10)
    except:
        await bot.answer_inline_query(query.id, [], cache_time=1)

async def build_content(first: Message, messages: list, status: Message):
    """собирает содержимое запроса: голос, текст, файл или картинки."""
    if first.voice or first.audio or first.video_note:
        try:
            transcript = await transcribe_message(first, status)
        except Exception as e:
            await status.edit_text(f"не смогла распознать голос: {e}")
            return None
        if not transcript:
            await status.edit_text(f"не расслышала {kao()}, напиши текстом или запиши ещё раз")
            return None
        caption = (first.caption or "").strip()
        return f"{caption}\n{transcript}".strip() if caption else transcript

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
            except Exception:
                pass
    if image_urls:
        content = [{"type": "text", "text": text or "опиши изображения"}]
        for url in image_urls:
            content.append({"type": "image_url", "image_url": {"url": url}})
        return content
    return text or None


# ---------- кнопочное меню ----------

def _label(text, limit: int = 30) -> str:
    return " ".join(str(text).split())[:limit]


def _span_short(starts_ts: int, ends_ts: int, tz) -> str:
    start = datetime.fromtimestamp(starts_ts, tz)
    end = datetime.fromtimestamp(ends_ts, tz)
    if start.date() == end.date():
        return f"{start:%d.%m %H:%M}–{end:%H:%M}"
    return f"{start:%d.%m %H:%M} – {end:%d.%m %H:%M}"


def _calendar_bounds(key: str):
    tz = ZoneInfo(SCHOOL_TZ)
    now = datetime.now(tz)
    if key == "today":
        start = datetime(now.year, now.month, now.day, tzinfo=tz)
        return start, start + timedelta(days=1), "сегодня"
    try:
        days = max(1, min(int(key), 365))
    except (TypeError, ValueError):
        days = 7
    return now, now + timedelta(days=days), f"{days} дн."


def calendar_payload(uid: int, key: str = "7"):
    start, end, label = _calendar_bounds(key)
    tz = ZoneInfo(SCHOOL_TZ)
    rows = db.list_events(uid, int(start.timestamp()), int(end.timestamp()))
    if rows:
        lines = [f"📅 календарь: {label}"]
        for e in rows[:12]:
            note = f"\n   {e['notes']}" if e["notes"] else ""
            lines.append(
                f"#{e['id']} — {_span_short(e['starts_ts'], e['ends_ts'], tz)} — {e['title']}{note}"
            )
        if len(rows) > 12:
            lines.append(f"… и ещё {len(rows) - 12}")
    else:
        lines = [f"📅 на {label} событий нет"]
    lines.append("\nдобавить: напиши «поставь встречу …»")

    buttons = [[
        InlineKeyboardButton(text="сегодня", callback_data="cal:days:today"),
        InlineKeyboardButton(text="7 дней", callback_data="cal:days:7"),
        InlineKeyboardButton(text="30 дней", callback_data="cal:days:30"),
    ]]
    for e in rows[:10]:
        buttons.append([InlineKeyboardButton(
            text=f"🗑 {_label(e['title'], 28)}",
            callback_data=f"cal:del:{e['id']}:{key}",
        )])
    buttons.append([InlineKeyboardButton(text="➕ добавить", callback_data="cal:add")])
    return "\n".join(lines)[:3900], InlineKeyboardMarkup(inline_keyboard=buttons)


def todos_payload(uid: int, include_done: bool = False):
    rows = db.list_todos(uid, include_done)
    if rows:
        lines = ["✅ дела:"]
        for t in rows:
            lines.append(f"{'✓' if t['done'] else '•'} #{t['id']} — {t['text']}")
    else:
        lines = ["✅ дел нет"]
    lines.append("\nдобавить: напиши «добавь дело …»")

    buttons = [[InlineKeyboardButton(text="➕ добавить", callback_data="todo:add")]]
    toggle_text = "скрыть выполненные" if include_done else "показать выполненные"
    buttons.append([InlineKeyboardButton(
        text=toggle_text, callback_data=f"todo:list:{0 if include_done else 1}"
    )])
    for t in rows:
        if not t["done"]:
            buttons.append([InlineKeyboardButton(
                text=f"✓ {_label(t['text'])}",
                callback_data=f"todo:done:{t['id']}:{1 if include_done else 0}",
            )])
    return "\n".join(lines)[:3900], InlineKeyboardMarkup(inline_keyboard=buttons)


def reminders_payload(uid: int):
    rows = db.list_reminders(uid)
    tz = ZoneInfo(SCHOOL_TZ)
    if rows:
        lines = ["⏰ напоминания:"]
        for r in rows:
            due = datetime.fromtimestamp(r["due_ts"], tz)
            repeat = {"daily": " · каждый день", "weekly": " · каждую неделю"}.get(r["repeat"], "")
            lines.append(f"#{r['id']} — {due:%d.%m %H:%M}{repeat} — {r['text']}")
    else:
        lines = ["⏰ напоминаний нет"]
    lines.append("\nдобавить: напиши «напомни …»")

    buttons = [[InlineKeyboardButton(text="➕ добавить", callback_data="rem:add")]]
    for r in rows[:10]:
        buttons.append([InlineKeyboardButton(
            text=f"🗑 {_label(r['text'])}", callback_data=f"rem:del:{r['id']}"
        )])
    return "\n".join(lines)[:3900], InlineKeyboardMarkup(inline_keyboard=buttons)


def memory_payload(uid: int):
    facts = db.list_facts(uid)
    summary = db.get_summary(uid)
    lines = ["🧠 память:"]
    if facts:
        for f in facts[:40]:
            lines.append(f"#{f['id']} — {f['text']}")
        if len(facts) > 40:
            lines.append(f"… и ещё {len(facts) - 40}")
    else:
        lines.append("фактов пока нет")
    if summary:
        lines.append(f"\nконспект прошлых разговоров: {len(summary['text'])} символов")
    lines.append("\nзапомнить: напиши «запомни, что …»")

    buttons = [[InlineKeyboardButton(text="➕ запомнить", callback_data="mem:add")]]
    if summary:
        buttons.append([InlineKeyboardButton(text="📝 показать конспект", callback_data="mem:summary")])
    if facts:
        buttons.append([InlineKeyboardButton(text="🧹 очистить факты", callback_data="mem:clear")])
    for f in facts[:8]:
        buttons.append([InlineKeyboardButton(
            text=f"🗑 {_label(f['text'])}", callback_data=f"mem:del:{f['id']}"
        )])
    return "\n".join(lines)[:3900], InlineKeyboardMarkup(inline_keyboard=buttons)


def settings_payload(uid: int):
    user = db.get_user(uid)
    briefing_on = db.get_setting("school_enabled", "1") == "1"
    effort = user["effort"] or DEFAULT_EFFORT
    lines = [
        "⚙️ настройки:",
        f"модель: {user['model'] or 'не выбрана'}",
        f"effort: {effort}",
        f"город: {user['city']}",
        f"инструменты: {'вкл' if user['tools'] else 'выкл'}",
        f"голос: {'вкл' if user['voice'] else 'выкл'}",
        f"брифинг: {'вкл' if briefing_on else 'выкл'} "
        f"({BRIEFING_WEEKDAY_TIME} будни / {BRIEFING_WEEKEND_TIME} выходные)",
    ]
    buttons = [
        [InlineKeyboardButton(text=f"🤖 модель: {user['model'] or 'выбрать'}", callback_data="set:model")],
        [InlineKeyboardButton(text=f"🧠 effort: {effort}", callback_data="set:effort")],
        [InlineKeyboardButton(text=f"🏙 город: {user['city']}", callback_data="set:city")],
        [InlineKeyboardButton(
            text=f"🔧 инструменты: {'вкл' if user['tools'] else 'выкл'}",
            callback_data="set:tools",
        )],
        [InlineKeyboardButton(
            text=f"🔊 голос: {'вкл' if user['voice'] else 'выкл'}",
            callback_data="set:voice",
        )],
        [InlineKeyboardButton(
            text=f"🌅 брифинг: {'вкл' if briefing_on else 'выкл'}",
            callback_data="set:briefing",
        )],
        [InlineKeyboardButton(text="📚 расписание", callback_data="set:schedule")],
        [InlineKeyboardButton(text="🧹 очистить историю", callback_data="set:clear")],
    ]
    return "\n".join(lines)[:3900], InlineKeyboardMarkup(inline_keyboard=buttons)


def schedule_payload():
    schedule = db.get_schedule()
    lines = ["📚 расписание:"]
    for day in WEEKDAYS:
        lines.append(f"{day}: {schedule.get(day) or '— пусто'}")
    buttons = []
    for i in range(0, len(WEEKDAYS), 2):
        row = [
            InlineKeyboardButton(text=WEEKDAYS[j], callback_data=f"sched:{j}")
            for j in range(i, min(i + 2, len(WEEKDAYS)))
        ]
        buttons.append(row)
    buttons.append([InlineKeyboardButton(text="⬅️ назад", callback_data="set:home")])
    return "\n\n".join(lines)[:3900], InlineKeyboardMarkup(inline_keyboard=buttons)


async def safe_edit(message: Message, text: str, markup=None):
    try:
        await message.edit_text(text, reply_markup=markup)
    except TelegramBadRequest as e:
        if "message is not modified" in str(e).lower():
            return
        await message.answer(text, reply_markup=markup)
    except Exception:
        await message.answer(text, reply_markup=markup)


async def show_settings(message: Message):
    text, markup = settings_payload(message.from_user.id)
    await message.answer(text, reply_markup=markup)


@dp.message(F.text == "📅 Календарь")
async def menu_calendar(message: Message):
    pending.pop(message.from_user.id, None)
    text, markup = calendar_payload(message.from_user.id)
    await message.answer(text, reply_markup=markup)


@dp.message(F.text == "✅ Дела")
async def menu_todos(message: Message):
    pending.pop(message.from_user.id, None)
    text, markup = todos_payload(message.from_user.id)
    await message.answer(text, reply_markup=markup)


@dp.message(F.text == "⏰ Напоминания")
async def menu_reminders(message: Message):
    pending.pop(message.from_user.id, None)
    text, markup = reminders_payload(message.from_user.id)
    await message.answer(text, reply_markup=markup)


@dp.message(F.text == "🧠 Память")
async def menu_memory(message: Message):
    pending.pop(message.from_user.id, None)
    text, markup = memory_payload(message.from_user.id)
    await message.answer(text, reply_markup=markup)


@dp.message(F.text == "⚙️ Настройки")
async def menu_settings(message: Message):
    pending.pop(message.from_user.id, None)
    await show_settings(message)


@dp.message(F.text == "🌅 Брифинг")
async def menu_briefing(message: Message):
    pending.pop(message.from_user.id, None)
    await message.answer(f"собираю брифинг {kao()}...")
    await morning_briefing(target_uid=message.from_user.id, force=True)


@dp.message(F.text == "❓ Помощь")
async def menu_help(message: Message):
    pending.pop(message.from_user.id, None)
    await message.answer(HELP_TEXT)


@dp.callback_query(F.data.startswith("cal:"))
async def cal_cb(callback: CallbackQuery):
    uid = callback.from_user.id
    parts = callback.data.split(":")
    if parts[1] == "add":
        await callback.answer("напиши: поставь встречу 30 октября в 16:00", show_alert=True)
        return
    if parts[1] == "days":
        key = parts[2] if len(parts) > 2 else "7"
        text, markup = calendar_payload(uid, key)
        await safe_edit(callback.message, text, markup)
    elif parts[1] == "del":
        try:
            eid = int(parts[2])
        except (ValueError, IndexError):
            await callback.answer("не поняла событие", show_alert=True)
            return
        key = parts[3] if len(parts) > 3 else "7"
        db.delete_event(uid, eid)
        text, markup = calendar_payload(uid, key)
        await safe_edit(callback.message, text, markup)
    await callback.answer()


@dp.callback_query(F.data.startswith("todo:"))
async def todo_cb(callback: CallbackQuery):
    uid = callback.from_user.id
    parts = callback.data.split(":")
    if parts[1] == "add":
        await callback.answer("напиши: добавь дело дочитать книгу", show_alert=True)
        return
    if parts[1] == "done":
        try:
            tid = int(parts[2])
        except (ValueError, IndexError):
            await callback.answer("не поняла дело", show_alert=True)
            return
        include_done = len(parts) > 3 and parts[3] == "1"
        db.complete_todo(uid, tid)
        text, markup = todos_payload(uid, include_done)
        await safe_edit(callback.message, text, markup)
    elif parts[1] == "list":
        include_done = len(parts) > 2 and parts[2] == "1"
        text, markup = todos_payload(uid, include_done)
        await safe_edit(callback.message, text, markup)
    await callback.answer()


@dp.callback_query(F.data.startswith("rem:"))
async def rem_cb(callback: CallbackQuery):
    uid = callback.from_user.id
    parts = callback.data.split(":")
    if parts[1] == "add":
        await callback.answer("напиши: напомни завтра в 9 про математику", show_alert=True)
        return
    if parts[1] == "del":
        try:
            rid = int(parts[2])
        except (ValueError, IndexError):
            await callback.answer("не поняла напоминание", show_alert=True)
            return
        reminder = db.get_reminder(rid)
        if reminder and reminder["uid"] == uid:
            db.delete_reminder(rid)
            sched.cancel_reminder_job(rid)
        text, markup = reminders_payload(uid)
        await safe_edit(callback.message, text, markup)
    await callback.answer()


@dp.callback_query(F.data.startswith("mem:"))
async def mem_cb(callback: CallbackQuery):
    uid = callback.from_user.id
    parts = callback.data.split(":")
    if parts[1] == "add":
        await callback.answer("напиши: запомни, что я люблю зелёный чай", show_alert=True)
        return
    if parts[1] == "summary":
        summary = db.get_summary(uid)
        await callback.message.answer(summary["text"] if summary else "конспекта пока нет")
        await callback.answer()
        return
    if parts[1] == "clear":
        markup = InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(text="да, очистить", callback_data="mem:clear_yes"),
            InlineKeyboardButton(text="отмена", callback_data="mem:back"),
        ]])
        await safe_edit(callback.message, "точно удалить все факты? конспект останется.", markup)
        await callback.answer()
        return
    if parts[1] == "clear_yes":
        db.clear_facts(uid)
    elif parts[1] == "del":
        try:
            fid = int(parts[2])
        except (ValueError, IndexError):
            await callback.answer("не поняла факт", show_alert=True)
            return
        db.delete_fact(uid, fid)
    text, markup = memory_payload(uid)
    await safe_edit(callback.message, text, markup)
    await callback.answer()


@dp.callback_query(F.data == "set:home")
async def set_home(callback: CallbackQuery):
    text, markup = settings_payload(callback.from_user.id)
    await safe_edit(callback.message, text, markup)
    await callback.answer()


@dp.callback_query(F.data == "set:model")
async def set_model(callback: CallbackQuery):
    try:
        models = await get_models()
    except Exception as e:
        await callback.answer(f"ошибка: {e}", show_alert=True)
        return
    markup = models_keyboard(models)
    markup.inline_keyboard.append(
        [InlineKeyboardButton(text="⬅️ назад", callback_data="set:home")]
    )
    await safe_edit(callback.message, "выбери модель:", markup)
    await callback.answer()


@dp.callback_query(F.data == "set:effort")
async def set_effort(callback: CallbackQuery):
    markup = InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="none", callback_data="eff:none"),
            InlineKeyboardButton(text="low", callback_data="eff:low"),
        ],
        [
            InlineKeyboardButton(text="high", callback_data="eff:high"),
            InlineKeyboardButton(text="max", callback_data="eff:max"),
        ],
        [InlineKeyboardButton(text="⬅️ назад", callback_data="set:home")],
    ])
    await safe_edit(callback.message, "глубина размышления:", markup)
    await callback.answer()


@dp.callback_query(F.data.startswith("eff:"))
async def eff_cb(callback: CallbackQuery):
    value = callback.data.split(":", 1)[1]
    if value in ("none", *THINKING_EFFORT_LEVELS):
        db.set_user(callback.from_user.id, effort=value)
    text, markup = settings_payload(callback.from_user.id)
    await safe_edit(callback.message, text, markup)
    await callback.answer()


@dp.callback_query(F.data == "set:city")
async def set_city(callback: CallbackQuery):
    pending[callback.from_user.id] = ("city", None)
    await callback.message.answer("пришли город одним сообщением, например: Москва")
    await callback.answer()


@dp.callback_query(F.data == "set:tools")
async def set_tools(callback: CallbackQuery):
    user = db.get_user(callback.from_user.id)
    db.set_user(callback.from_user.id, tools=0 if user["tools"] else 1)
    text, markup = settings_payload(callback.from_user.id)
    await safe_edit(callback.message, text, markup)
    await callback.answer()


@dp.callback_query(F.data == "set:voice")
async def set_voice(callback: CallbackQuery):
    user = db.get_user(callback.from_user.id)
    db.set_user(callback.from_user.id, voice=0 if user["voice"] else 1)
    text, markup = settings_payload(callback.from_user.id)
    await safe_edit(callback.message, text, markup)
    await callback.answer()


@dp.callback_query(F.data == "set:briefing")
async def set_briefing(callback: CallbackQuery):
    current = db.get_setting("school_enabled", "1")
    db.set_setting("school_enabled", "0" if current == "1" else "1")
    text, markup = settings_payload(callback.from_user.id)
    await safe_edit(callback.message, text, markup)
    await callback.answer()


@dp.callback_query(F.data == "set:schedule")
async def set_schedule(callback: CallbackQuery):
    text, markup = schedule_payload()
    await safe_edit(callback.message, text, markup)
    await callback.answer()


@dp.callback_query(F.data.startswith("sched:"))
async def sched_cb(callback: CallbackQuery):
    try:
        idx = int(callback.data.split(":", 1)[1])
        day = WEEKDAYS[idx]
    except (ValueError, IndexError):
        await callback.answer("не поняла день", show_alert=True)
        return
    pending[callback.from_user.id] = ("schedule", day)
    await callback.message.answer(
        f"пришли расписание на {day} одним сообщением (или «-», чтобы очистить)"
    )
    await callback.answer()


@dp.callback_query(F.data == "set:clear")
async def set_clear(callback: CallbackQuery):
    markup = InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="да, очистить", callback_data="set:clear_yes"),
        InlineKeyboardButton(text="отмена", callback_data="set:home"),
    ]])
    await safe_edit(
        callback.message,
        "очистить историю и конспект? факты о тебе останутся.",
        markup,
    )
    await callback.answer()


@dp.callback_query(F.data == "set:clear_yes")
async def set_clear_yes(callback: CallbackQuery):
    db.clear_history(callback.from_user.id)
    text, markup = settings_payload(callback.from_user.id)
    await safe_edit(callback.message, "история очищена.\n\n" + text, markup)
    await callback.answer()


async def process_single_or_album(messages: list):
    first = messages[0]
    uid = first.from_user.id
    user = db.get_user(uid)
    if not user["model"]:
        await first.answer(f"сначала выбери модель через /start или /models {kao()}")
        return
    model = user["model"]

    is_voice = bool(first.voice or first.audio or first.video_note)
    status_text = "слушаю…" if is_voice else (
        "думаю... ▌" if (user["effort"] or DEFAULT_EFFORT) != "none" else "▌"
    )
    status = await first.answer(status_text)

    try:
        content = await build_content(first, messages, status)
        if content is None:
            return
        convo = [{"role": "system", "content": build_system_prompt(uid)}]
        convo.extend(db.get_context(uid))
        convo.append({"role": "user", "content": content})
        db.append_message(uid, "user", content_for_storage(content))
        full, voice_requested = await run_agent(model, convo, uid, status, bool(user["tools"]))
    except Exception as e:
        await status.edit_text(f"ошибка: {e}")
        return

    full = full.strip() or "…"
    db.append_message(uid, "assistant", full)
    chunks = [full[i:i + 4000] for i in range(0, len(full), 4000)]
    try:
        if len(chunks) == 1:
            await status.edit_text(chunks[0], reply_markup=change_model_keyboard())
        else:
            await status.edit_text(chunks[0])
            for i, chunk in enumerate(chunks[1:], start=1):
                markup = change_model_keyboard() if i == len(chunks) - 1 else None
                await first.answer(chunk, reply_markup=markup)
    except Exception:
        await first.answer(full[:4000], reply_markup=change_model_keyboard())

    if bool(user["voice"]) or voice_requested:
        await send_voice_reply(first, full)

    spawn(memory.after_turn(uid))

async def wait_and_process_album(gid: str):
    await asyncio.sleep(1.0)
    msgs = album_buffer.pop(gid, [])
    album_tasks.pop(gid, None)
    if msgs:
        msgs.sort(key=lambda m: m.message_id)
        await process_single_or_album(msgs)

@dp.message(F.photo | F.document | F.voice | F.audio | F.video_note | F.text | F.caption)
async def handle_prompt(message: Message):
    uid = message.from_user.id
    action = pending.pop(uid, None)
    if action and message.text:
        kind, extra = action
        text = message.text.strip()
        if kind == "city" and text:
            db.set_user(uid, city=text[:80])
            await message.answer(f"город сохранён: {text[:80]} {kao()}")
            await show_settings(message)
            return
        if kind == "schedule":
            db.set_schedule(extra, "" if text == "-" else text)
            await message.answer(f"расписание на {extra} сохранено {kao()}")
            text2, markup = schedule_payload()
            await message.answer(text2, reply_markup=markup)
            return

    if message.media_group_id:
        gid = message.media_group_id
        if gid not in album_buffer:
            album_buffer[gid] = []
            album_tasks[gid] = asyncio.create_task(wait_and_process_album(gid))
        album_buffer[gid].append(message)
        return
    await process_single_or_album([message])

def parse_hm(value: str, default: tuple[int, int]) -> tuple[int, int]:
    try:
        h, m = (int(part) for part in value.split(":"))
        if 0 <= h <= 23 and 0 <= m <= 59:
            return h, m
    except (ValueError, TypeError):
        pass
    return default


async def main():
    db.init(DEFAULT_SCHEDULE)
    sched.init(bot)
    sched.start()
    tz = ZoneInfo(SCHOOL_TZ)
    wd_h, wd_m = parse_hm(BRIEFING_WEEKDAY_TIME, (6, 30))
    we_h, we_m = parse_hm(BRIEFING_WEEKEND_TIME, (9, 0))
    sched.aps.add_job(
        morning_briefing,
        CronTrigger(day_of_week="mon-fri", hour=wd_h, minute=wd_m, timezone=tz),
        id="briefing_weekday",
        replace_existing=True,
    )
    sched.aps.add_job(
        morning_briefing,
        CronTrigger(day_of_week="sat,sun", hour=we_h, minute=we_m, timezone=tz),
        id="briefing_weekend",
        replace_existing=True,
    )
    sched.restore_reminders()
    spawn(voice.warmup())
    await bot.delete_webhook(drop_pending_updates=True)
    await set_commands()
    print("бот запущен...")
    try:
        await dp.start_polling(bot)
    finally:
        sched.shutdown()

if __name__ == "__main__":
    asyncio.run(main())