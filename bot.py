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

env_path = Path(__file__).parent / ".env"
load_dotenv(dotenv_path=env_path)

BOT_TOKEN = os.getenv("BOT_TOKEN")
RADOX_API_KEY = os.getenv("RADOX_API_KEY")
RADOX_BASE_URL = os.getenv("RADOX_BASE_URL", "https://radoxai.com/v1")

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


def models_keyboard(models: list):
    builder = InlineKeyboardBuilder()
    for m in models:
        builder.button(text=m, callback_data=f"model:{m}")
    builder.adjust(2)
    return builder.as_markup()


def change_model_keyboard():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🔄 Сменить модель", callback_data="change_model")]
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
        BotCommand(command="start", description="🚀 Запустить и выбрать модель"),
        BotCommand(command="models", description="🔄 Сменить модель"),
        BotCommand(command="clear", description="🧹 Очистить историю"),
    ])


@dp.message(CommandStart())
async def start(message: Message):
    await message.answer("Привет! Загружаю список моделей...")
    try:
        models = await get_models()
        await message.answer("Выбери модель:", reply_markup=models_keyboard(models))
    except Exception as e:
        await message.answer(f"Ошибка: {e}")


@dp.message(Command("models"))
async def show_models(message: Message):
    models = await get_models()
    await message.answer("Выбери новую модель:", reply_markup=models_keyboard(models))


@dp.message(Command("clear"))
async def clear_history(message: Message):
    user_history[message.from_user.id] = []
    await message.answer("Контекст очищен 🧹")


@dp.callback_query(F.data.startswith("model:"))
async def select_model(callback: CallbackQuery):
    model = callback.data.split("model:", 1)[1]
    user_model[callback.from_user.id] = model
    user_history[callback.from_user.id] = []
    await callback.message.edit_text(f"✅ Выбрана: `{model}`\nТеперь просто напиши запрос!", parse_mode="Markdown")
    await callback.answer()


@dp.callback_query(F.data == "change_model")
async def change_model_btn(callback: CallbackQuery):
    models = await get_models()
    await callback.message.answer("Выбери новую модель:", reply_markup=models_keyboard(models))
    await callback.answer()


@dp.message(F.text)
async def handle_prompt(message: Message):
    uid = message.from_user.id
    if uid not in user_model:
        await message.answer("Сначала выбери модель через /start или /models")
        return

    model = user_model[uid]
    if uid not in user_history:
        user_history[uid] = []
    user_history[uid].append({"role": "user", "content": message.text})

    msg = await message.answer(f"🧠 Печатаю через `{model}`... ▌", parse_mode="Markdown")

    full_text = ""
    final_usage = {}
    last_edit = 0

    try:
        async for delta, usage in stream_chat(model, user_history[uid]):
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

        user_history[uid].append({"role": "assistant", "content": full_text})

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
        await msg.edit_text(f"❌ Ошибка: {e}")


async def main():
    await set_commands()
    print("Бот запущен и меню установлено...")
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())