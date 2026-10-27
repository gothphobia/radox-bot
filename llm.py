"""небольшой не-стриминговый клиент для фоновых задач: память, конспекты."""

import os
import ssl
from pathlib import Path

import aiohttp
import certifi
from dotenv import load_dotenv

load_dotenv(dotenv_path=Path(__file__).parent / ".env")

API_KEY = os.getenv("DEEPSEEK_API_KEY")
BASE_URL = os.getenv("DEEPSEEK_BASE_URL", "https://api.deepseek.com")
DEFAULT_MODEL = os.getenv("MEMORY_MODEL", "deepseek-flash")
_SSL = ssl.create_default_context(cafile=certifi.where())


async def complete(messages: list, model: str | None = None, max_tokens: int = 400) -> str:
    payload = {
        "model": model or DEFAULT_MODEL,
        "messages": messages,
        "stream": False,
        "thinking": {"type": "disabled"},
        "max_tokens": max_tokens,
    }
    headers = {"Authorization": f"Bearer {API_KEY}", "Content-Type": "application/json"}
    connector = aiohttp.TCPConnector(ssl=_SSL)
    timeout = aiohttp.ClientTimeout(total=60)
    async with aiohttp.ClientSession(connector=connector, timeout=timeout) as session:
        async with session.post(
            f"{BASE_URL}/chat/completions", headers=headers, json=payload
        ) as resp:
            if resp.status != 200:
                raise RuntimeError(f"ошибка API {resp.status}: {await resp.text()}")
            data = await resp.json()
            return (data["choices"][0]["message"].get("content") or "").strip()
