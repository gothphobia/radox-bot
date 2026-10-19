import os
import json
import asyncio
import time
import ssl
import certifi
import base64
import aiohttp
from io import BytesIO
from pathlib import Path
from dotenv import load_dotenv

from aiogram import Bot, Dispatcher, F
from aiogram.types import Message, CallbackQuery, BotCommand, InlineKeyboardButton, InlineKeyboardMarkup, Update
from aiogram.filters import CommandStart, Command
from aiogram.utils.keyboard import InlineKeyboardBuilder
from aiogram.dispatcher.middlewares.base import BaseMiddleware

env_path = Path(__file__).parent / ".env"
load_dotenv(dotenv_path=env_path)

BOT_TOKEN = os.getenv("BOT_TOKEN")
RADOX_API_KEY = os.getenv("RADOX_API_KEY")
RADOX_BASE_URL = os.getenv("RADOX_BASE_URL", "https://api.radox.ai/v1")

SYSTEM_PROMPT = """ты — спокойная аниме-девушка. пиши только с маленькой буквы, каомодзи :3, ^^, xd умеренно. без слащавости. на серьезные запросы — обычный грамотный стиль без отыгрыша."""

ALLOWED_USERNAME = "gothphobia"

if not BOT_TOKEN:
    raise ValueError(f".env не найден по пути {env_path}")

bot = Bot(token=BOT_TOKEN)
dp = Dispatcher()

user_model = {}
user_history = {}

HEADERS = {
    "Authorization": f"Bearer {RADOX_API_KEY}",
    "Content-Type": "application/json"
}

ssl_context = ssl.create_default_context(cafile=certifi.where())

# --- БЛОКИРОВКА ПОСЛЕ 1 ПРЕДУПРЕЖДЕНИЯ, НЕПРОБИВАЕМАЯ ---
BLOCKED_FILE = Path(__file__).parent / "blocked.json"
already_warned = set()
if BLOCKED_FILE.exists():
    try:
        already_warned = set(json.loads(BLOCKED_FILE.read_text(encoding="utf-8")))
    except:
        already_warned = set()

def save_blocked():
    try:
        BLOCKED_FILE.write_text(json.dumps(list(already_warned)), encoding="utf-8")
    except:
        pass

def is_allowed(user) -> bool:
    if not user or not user.username:
        return False
    return user.username.lower() == ALLOWED_USERNAME.lower()

class WhitelistMiddleware(BaseMiddleware):
    async def __call__(self, handler, event, data):
        user = None
        # определяем юзера из любого типа апдейта
        if isinstance(event, Update):
            if event.message:
                user = event.message.from_user
            elif event.edited_message:
                user = event.edited_message.from_user
            elif event.callback_query:
                user = event.callback_query.from_user
            elif event.inline_query:
                user = event.inline_query.from_user
            elif event.channel_post:
                user = event.channel_post.from_user
            elif event.edited_channel_post:
                user = event.edited_channel_post.from_user
        elif isinstance(event, Message):
            user = event.from_user
        elif isinstance(event, CallbackQuery):
            user = event.from_user

        if user is None:
            return await handler(event, data)

        if is_allowed(user):
            return await handler(event, data)

        # не gothphobia
        if user.id in already_warned:
            return  # полный игнор, даже без ответа

        # первый раз - отвечаем 1 раз и запоминаем
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
            elif isinstance(event, Message):
                await event.answer("ничем не могу помочь xd")
            elif isinstance(event, CallbackQuery):
                await event.answer("ничем не могу помочь xd", show_alert=True)
        except:
            pass
        return

# ставим на самый верхний уровень - режет все до хэндлеров и до нейросети
dp.update.outer_middleware(WhitelistMiddleware())

def get_history_with_system(uid: int):
    if uid not in user_history or not user_history[uid]:
        user_history[uid] = [{"role": "system", "content": SYSTEM_PROMPT}]
    if user_history[uid][0].get("role") != "system":
        user_history[uid].insert(0, {"role": "system", "content": SYSTEM_PROMPT})
    else:
        user_history[uid][0]["content"] = SYSTEM_PROMPT
    return user_history[uid]

def models_keyboard(models: list):
    builder = InlineKeyboardBuilder()
    for m in models:
        builder.button(text=m, callback_data=f"model:{m}")
    builder.adjust(2)
    return builder.as_markup()

def change_model_keyboard():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="сменить модель :3", callback_data="change_model")]
    ])

async def get_models():
    connector = aiohttp.TCPConnector(ssl=ssl_context)
    async with aiohttp.ClientSession(connector=connector) as session:
        async with session.get(f"{RADOX_BASE_URL}/models", headers=HEADERS) as resp:
            data = await resp.json()
            if "data" in data:
                return [m["id"] for m in data["data"]]
            return data

async def stream_chat(model: str, messages: list):
    payload = {
        "model": model,
        "messages": messages,
        "stream": True,
        "stream_options": {"include_usage": True}
    }
    connector = aiohttp.TCPConnector(ssl=ssl_context)
    async with aiohttp.ClientSession(connector=connector) as session:
        async with session.post(f"{RADOX_BASE_URL}/chat/completions", headers=HEADERS, json=payload) as resp:
            if resp.status != 200:
                data = await resp.text()
                raise Exception(f"Ошибка API {resp.status}: {data}")
            usage = {}
            async for line in resp.content:
                line = line.decode("utf-8").strip()
                if not line or line == "data: [DONE]":
                    continue
                if line.startswith("data: "):
                    json_str = line[6:]
                    try:
                        chunk = json.loads(json_str)
                        if "usage" in chunk and chunk["usage"]:
                            usage = chunk["usage"]
                        delta = chunk["choices"][0].get("delta", {}).get("content", "")
                        if delta:
                            yield delta, None
                    except:
                        continue
            yield "", usage

async def photo_to_base64(message: Message):
    file_id = None
    if message.photo:
        file_id = message.photo[-1].file_id
    elif message.document and message.document.mime_type and message.document.mime_type.startswith("image/"):
        file_id = message.document.file_id
    else:
        return None
    file = await bot.get_file(file_id)
    buf = BytesIO()
    await bot.download(file, buf)
    buf.seek(0)
    b64 = base64.b64encode(buf.read()).decode("utf-8")
    mime = "image/jpeg"
    if message.document and message.document.mime_type:
        mime = message.document.mime_type
    return f"data:{mime};base64,{b64}"

async def set_commands():
    await bot.set_my_commands([
        BotCommand(command="start", description="запустить и выбрать модель ^^"),
        BotCommand(command="models", description="сменить модель :3"),
        BotCommand(command="clear", description="очистить историю"),
    ])

@dp.message(CommandStart())
async def start(message: Message):
    await message.answer("привет ^^ загружаю список моделей")
    try:
        models = await get_models()
        await message.answer("выбери модель ^-^", reply_markup=models_keyboard(models))
    except Exception as e:
        await message.answer(f"ошибка: {e}")

@dp.message(Command("models"))
async def show_models(message: Message):
    models = await get_models()
    await message.answer("выбери модель ^-^", reply_markup=models_keyboard(models))

@dp.message(Command("clear"))
async def clear_history(message: Message):
    user_history[message.from_user.id] = [{"role": "system", "content": SYSTEM_PROMPT}]
    await message.answer("история очищена, можем начать заново :3")

@dp.callback_query(F.data.startswith("model:"))
async def select_model(callback: CallbackQuery):
    model = callback.data.split("model:", 1)[1]
    user_model[callback.from_user.id] = model
    user_history[callback.from_user.id] = [{"role": "system", "content": SYSTEM_PROMPT}]
    await callback.message.edit_text(f"выбрана `{model}` ^^ теперь напиши сообщение", parse_mode="Markdown")
    await callback.answer()

@dp.callback_query(F.data == "change_model")
async def change_model_btn(callback: CallbackQuery):
    models = await get_models()
    await callback.message.answer("выбери другую модель :d", reply_markup=models_keyboard(models))
    await callback.answer()

@dp.message(F.photo | F.document | F.text)
async def handle_prompt(message: Message):
    uid = message.from_user.id
    if uid not in user_model:
        await message.answer("сначала выбери модель через /start или /models")
        return
    model = user_model[uid]
    history = get_history_with_system(uid)

    text = message.caption or message.text or ""
    if not text and message.photo:
        text = "что на этом изображении?"

    image_url = None
    if message.photo or (message.document and message.document.mime_type and message.document.mime_type.startswith("image/")):
        try:
            image_url = await photo_to_base64(message)
        except Exception as e:
            await message.answer(f"не смогла скачать картинку: {e}")
            return

    if image_url:
        user_content = [
            {"type": "text", "text": text},
            {"type": "image_url", "image_url": {"url": image_url}}
        ]
        history.append({"role": "user", "content": user_content})
    else:
        if not text:
            return
        history.append({"role": "user", "content": text})

    msg = await message.answer(f"▌", parse_mode="Markdown")
    full_text = ""
    final_usage = {}
    last_edit = 0
    try:
        async for delta, usage in stream_chat(model, history):
            if usage:
                final_usage = usage
            if delta:
                full_text += delta
                if time.time() - last_edit > 0.4:
                    try:
                        await msg.edit_text(full_text + " ▌")
                        last_edit = time.time()
                    except:
                        pass
        history.append({"role": "assistant", "content": full_text})
        prompt_tokens = final_usage.get("prompt_tokens", "-")
        completion_tokens = final_usage.get("completion_tokens", "-")
        total_tokens = final_usage.get("total_tokens", "-")
        final_text = (
            f"{full_text}\n\n"
            f"━━━━━━━━━━━━━━━\n"
            f"🤖 `{model}` | 📊 `prompt: {prompt_tokens} | comp: {completion_tokens} | total: {total_tokens}`"
        )
        if len(final_text) > 4096:
            await msg.edit_text(full_text[:4096])
            await message.answer(
                f"━━━━━━━━━━━━━━━\n🤖 `{model}` | 📊 `total: {total_tokens}`",
                parse_mode="Markdown",
                reply_markup=change_model_keyboard()
            )
        else:
            await msg.edit_text(final_text, parse_mode="Markdown", reply_markup=change_model_keyboard())
    except Exception as e:
        await msg.edit_text(f"ошибка: {e}")

async def main():
    await bot.delete_webhook(drop_pending_updates=True)
    await set_commands()
    print("Бот запущен...")
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())