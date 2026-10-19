import os
import json
import asyncio
import time
import ssl
import certifi
import aiohttp
from pathlib import Path
from dotenv import load_dotenv

from aiogram import Bot, Dispatcher, F
from aiogram.types import Message, CallbackQuery, BotCommand, InlineKeyboardButton, InlineKeyboardMarkup
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

# --- БЛОКИРОВКА ПОСЛЕ 1 ПРЕДУПРЕЖДЕНИЯ ---
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
        user = getattr(event, "from_user", None)
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
        if isinstance(event, Message):
            await event.answer("ничем не могу помочь xd")
        elif isinstance(event, CallbackQuery):
            try:
                if event.message:
                    await event.message.answer("ничем не могу помочь xd")
            except:
                pass
            await event.answer("ничем не могу помочь xd", show_alert=True)
        return


# подключаем проверку на все сообщения и кнопки
dp.message.middleware(WhitelistMiddleware())
dp.callback_query.middleware(WhitelistMiddleware())


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


@dp.message(F.text)
async def handle_prompt(message: Message):
    uid = message.from_user.id
    if uid not in user_model:
        await message.answer("сначала выбери модель через /start или /models")
        return
    model = user_model[uid]
    history = get_history_with_system(uid)
    history.append({"role": "user", "content": message.text})
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