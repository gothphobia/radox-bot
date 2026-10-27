"""инструменты, которые модель может вызывать сама (function calling)."""

import asyncio
import ipaddress
import json
import os
import re
import socket
import ssl
from dataclasses import dataclass
from datetime import datetime, timedelta
from html.parser import HTMLParser
from io import BytesIO
from urllib.parse import urljoin, urlsplit
from zoneinfo import ZoneInfo

import aiohttp
import certifi

import db
import scheduler as sched

TAVILY_API_KEY = os.getenv("TAVILY_API_KEY")
USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"
)

WEEKDAYS = ["понедельник", "вторник", "среда", "четверг", "пятница", "суббота", "воскресенье"]
WEEKDAYS_SHORT = ["пн", "вт", "ср", "чт", "пт", "сб", "вс"]
MONTHS = ["января", "февраля", "марта", "апреля", "мая", "июня", "июля", "августа", "сентября", "октября", "ноября", "декабря"]

WEATHER_CODES = {
    0: "ясно", 1: "почти ясно", 2: "переменная облачность", 3: "пасмурно",
    45: "туман", 48: "изморозь",
    51: "слабая морось", 53: "морось", 55: "сильная морось",
    56: "ледяная морось", 57: "сильная ледяная морось",
    61: "небольшой дождь", 63: "дождь", 65: "сильный дождь",
    66: "ледяной дождь", 67: "сильный ледяной дождь",
    71: "небольшой снег", 73: "снег", 75: "сильный снег", 77: "снежная крупа",
    80: "небольшие ливни", 81: "ливни", 82: "сильные ливни",
    85: "снегопады", 86: "сильные снегопады",
    95: "гроза", 96: "гроза с градом", 99: "сильная гроза с градом",
}

REPEAT_ALIASES = {
    "none": "none", "no": "none", "нет": "none", "разово": "none",
    "daily": "daily", "day": "daily", "ежедневно": "daily", "каждый день": "daily",
    "weekly": "weekly", "week": "weekly", "еженедельно": "weekly", "каждую неделю": "weekly",
}


@dataclass
class ToolContext:
    uid: int
    tz: ZoneInfo
    city: str = "Севастополь"
    voice_requested: bool = False


# ---------- схемы для api ----------

def _tool(name: str, description: str, properties: dict, required: list[str]) -> dict:
    return {
        "type": "function",
        "function": {
            "name": name,
            "description": description,
            "parameters": {
                "type": "object",
                "properties": properties,
                "required": required,
            },
        },
    }


TOOL_SCHEMAS = [
    _tool(
        "create_reminder",
        "Создать напоминание. Используй, когда просят напомнить о чём-то. "
        "due_at — точное локальное время в формате ISO 8601, например 2026-10-28T09:00.",
        {
            "text": {"type": "string", "description": "о чём напомнить"},
            "due_at": {"type": "string", "description": "дата и время ISO 8601 без часового пояса (локальное время пользователя)"},
            "repeat": {"type": "string", "enum": ["none", "daily", "weekly"], "description": "повтор: none, daily или weekly"},
        },
        ["text", "due_at"],
    ),
    _tool(
        "list_reminders",
        "Показать все активные напоминания пользователя.",
        {},
        [],
    ),
    _tool(
        "cancel_reminder",
        "Отменить напоминание по его номеру.",
        {"id": {"type": "integer", "description": "номер напоминания из списка"}},
        ["id"],
    ),
    _tool(
        "add_todo",
        "Добавить дело в список дел (без конкретного времени).",
        {"text": {"type": "string", "description": "что нужно сделать"}},
        ["text"],
    ),
    _tool(
        "list_todos",
        "Показать список дел пользователя.",
        {"include_done": {"type": "boolean", "description": "показать и выполненные тоже"}},
        [],
    ),
    _tool(
        "complete_todo",
        "Отметить дело выполненным по номеру.",
        {"id": {"type": "integer", "description": "номер дела из списка"}},
        ["id"],
    ),
    _tool(
        "add_event",
        "Добавить событие в календарь. Используй, когда просят поставить встречу, занятие или дело на конкретное время.",
        {
            "title": {"type": "string", "description": "название события"},
            "start": {"type": "string", "description": "начало, ISO 8601 в локальном времени, например 2026-10-29T16:00"},
            "end": {"type": "string", "description": "окончание, ISO 8601; если не указано — через час"},
            "notes": {"type": "string", "description": "заметка к событию"},
        },
        ["title", "start"],
    ),
    _tool(
        "list_events",
        "Показать события календаря на ближайшие дни.",
        {
            "days": {"type": "integer", "description": "на сколько дней вперёд, по умолчанию 7"},
            "from_date": {"type": "string", "description": "начало диапазона ISO 8601; пусто — сейчас"},
        },
        [],
    ),
    _tool(
        "delete_event",
        "Удалить событие из календаря по номеру.",
        {"id": {"type": "integer", "description": "номер события из списка"}},
        ["id"],
    ),
    _tool(
        "remember",
        "Сохранить долговременный факт о пользователе. Используй, когда просят запомнить что-то важное (предпочтения, цели, обстоятельства).",
        {"fact": {"type": "string", "description": "короткий факт от третьего лица"}},
        ["fact"],
    ),
    _tool(
        "list_facts",
        "Показать, что бот помнит о пользователе.",
        {},
        [],
    ),
    _tool(
        "forget",
        "Удалить факт из долговременной памяти по номеру.",
        {"id": {"type": "integer", "description": "номер факта из списка"}},
        ["id"],
    ),
    _tool(
        "request_voice_reply",
        "Включить голосовой ответ на текущее сообщение (женским голосом). Используй только если пользователь прямо просит ответить голосом, озвучить или сказать вслух. По умолчанию отвечай текстом.",
        {},
        [],
    ),
    _tool(
        "get_weather",
        "Узнать текущую погоду и прогноз на сегодня и завтра. Если город не указан — берётся город пользователя.",
        {"city": {"type": "string", "description": "название города"}},
        [],
    ),
    _tool(
        "web_search",
        "Найти информацию в интернете. Используй для свежих данных, новостей, цен, фактов, которых нет в памяти.",
        {
            "query": {"type": "string", "description": "поисковый запрос"},
            "max_results": {"type": "integer", "description": "1–10, по умолчанию 5"},
        },
        ["query"],
    ),
    _tool(
        "fetch_page",
        "Открыть страницу по ссылке и прочитать её текст. Используй после web_search, если нужны детали.",
        {"url": {"type": "string", "description": "полная ссылка http(s)"}},
        ["url"],
    ),
    _tool(
        "get_schedule",
        "Посмотреть школьное расписание на день.",
        {"day": {"type": "string", "description": "понедельник…воскресенье, «сегодня» или «завтра»; пусто — сегодня"}},
        [],
    ),
    _tool(
        "get_current_datetime",
        "Узнать текущие дату, время и день недели. Используй перед расчётом относительных дат («завтра», «в пятницу»).",
        {},
        [],
    ),
]


# ---------- вспомогательное ----------

def _human_ts(ts: int, tz: ZoneInfo) -> str:
    dt = datetime.fromtimestamp(ts, tz)
    return f"{dt.day:02d}.{dt.month:02d} {dt.hour:02d}:{dt.minute:02d} ({WEEKDAYS_SHORT[dt.weekday()]})"


def _parse_due(raw: str, tz: ZoneInfo) -> datetime | None:
    raw = (raw or "").strip()
    if not raw:
        return None
    dt = None
    try:
        dt = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        pass
    if dt is None:
        now = datetime.now(tz)
        for fmt in ("%d.%m.%Y %H:%M", "%d.%m.%Y", "%d.%m %H:%M", "%d.%m"):
            try:
                dt = datetime.strptime(raw, fmt)
                if "%Y" not in fmt:
                    dt = dt.replace(year=now.year)
                break
            except ValueError:
                continue
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=tz)
    return dt


def _event_span(starts_ts: int, ends_ts: int, tz: ZoneInfo) -> str:
    start = datetime.fromtimestamp(starts_ts, tz)
    end = datetime.fromtimestamp(ends_ts, tz)
    if start.date() == end.date():
        return f"{start:%d.%m %H:%M}–{end:%H:%M} ({WEEKDAYS_SHORT[start.weekday()]})"
    return f"{start:%d.%m %H:%M} – {end:%d.%m %H:%M}"


def _normalize_repeat(value) -> str:
    return REPEAT_ALIASES.get(str(value or "none").strip().lower(), "none")


async def _get_json(url: str, params: dict | None = None) -> dict:
    ctx = ssl.create_default_context(cafile=certifi.where())
    connector = aiohttp.TCPConnector(ssl=ctx)
    timeout = aiohttp.ClientTimeout(total=15)
    async with aiohttp.ClientSession(connector=connector, timeout=timeout) as session:
        async with session.get(url, params=params) as resp:
            resp.raise_for_status()
            return await resp.json()


# ---------- реализация инструментов ----------

async def _create_reminder(args: dict, ctx: ToolContext) -> str:
    text = str(args.get("text", "")).strip()
    if not text:
        return "ошибка: пустой текст напоминания"
    due = _parse_due(str(args.get("due_at", "")), ctx.tz)
    if due is None:
        return "ошибка: не поняла дату, нужен формат ISO 8601, например 2026-10-28T09:00"
    if due.timestamp() <= datetime.now(ctx.tz).timestamp():
        return "ошибка: это время уже прошло, уточни дату и время"
    repeat = _normalize_repeat(args.get("repeat"))
    rid = db.add_reminder(ctx.uid, text, int(due.timestamp()), repeat)
    reminder = db.get_reminder(rid)
    sched.schedule_reminder(reminder)
    suffix = {"daily": ", каждый день", "weekly": ", каждую неделю"}.get(repeat, "")
    return f"напоминание #{rid} создано на {_human_ts(reminder['due_ts'], ctx.tz)}{suffix}: {text}"


async def _list_reminders(args: dict, ctx: ToolContext) -> str:
    rows = db.list_reminders(ctx.uid)
    if not rows:
        return "активных напоминаний нет"
    lines = ["активные напоминания:"]
    for r in rows:
        repeat = {"daily": " (каждый день)", "weekly": " (каждую неделю)"}.get(r["repeat"], "")
        lines.append(f"#{r['id']} — {_human_ts(r['due_ts'], ctx.tz)}{repeat}: {r['text']}")
    return "\n".join(lines)


async def _cancel_reminder(args: dict, ctx: ToolContext) -> str:
    try:
        rid = int(args.get("id"))
    except (TypeError, ValueError):
        return "ошибка: нужен номер напоминания"
    reminder = db.get_reminder(rid)
    if not reminder or reminder["uid"] != ctx.uid:
        return f"напоминание #{rid} не найдено"
    db.delete_reminder(rid)
    sched.cancel_reminder_job(rid)
    return f"напоминание #{rid} отменено: {reminder['text']}"


async def _add_todo(args: dict, ctx: ToolContext) -> str:
    text = str(args.get("text", "")).strip()
    if not text:
        return "ошибка: пустое дело"
    tid = db.add_todo(ctx.uid, text)
    return f"дело #{tid} добавлено: {text}"


async def _list_todos(args: dict, ctx: ToolContext) -> str:
    include_done = bool(args.get("include_done", False))
    rows = db.list_todos(ctx.uid, include_done)
    if not rows:
        return "список дел пуст"
    lines = ["дела:"]
    for t in rows:
        mark = "✓" if t["done"] else "•"
        lines.append(f"{mark} #{t['id']} — {t['text']}")
    return "\n".join(lines)


async def _complete_todo(args: dict, ctx: ToolContext) -> str:
    try:
        tid = int(args.get("id"))
    except (TypeError, ValueError):
        return "ошибка: нужен номер дела"
    if db.complete_todo(ctx.uid, tid):
        return f"дело #{tid} отмечено выполненным"
    return f"дело #{tid} не найдено"


async def _add_event(args: dict, ctx: ToolContext) -> str:
    title = str(args.get("title", "")).strip()
    if not title:
        return "ошибка: пустое название события"
    start = _parse_due(str(args.get("start", "")), ctx.tz)
    if start is None:
        return "ошибка: не поняла начало, нужен ISO 8601, например 2026-10-29T16:00"
    end_raw = str(args.get("end", "")).strip()
    end = _parse_due(end_raw, ctx.tz) if end_raw else start + timedelta(hours=1)
    if end is None:
        return "ошибка: не поняла окончание события"
    if end.timestamp() <= start.timestamp():
        return "ошибка: окончание должно быть позже начала"
    notes = str(args.get("notes", "")).strip()[:1000]
    starts_ts, ends_ts = int(start.timestamp()), int(end.timestamp())
    eid = db.add_event(ctx.uid, title[:300], starts_ts, ends_ts, notes)
    return f"событие #{eid} добавлено: {_event_span(starts_ts, ends_ts, ctx.tz)} — {title}"


async def _list_events(args: dict, ctx: ToolContext) -> str:
    try:
        days = int(args.get("days", 7))
    except (TypeError, ValueError):
        days = 7
    days = max(1, min(days, 365))
    from_raw = str(args.get("from_date", "")).strip()
    start = _parse_due(from_raw, ctx.tz) if from_raw else datetime.now(ctx.tz)
    if start is None:
        return "ошибка: не поняла дату начала"
    rows = db.list_events(
        ctx.uid,
        int(start.timestamp()),
        int((start + timedelta(days=days)).timestamp()),
    )
    if not rows:
        return f"событий нет на {days} дн. вперёд"
    lines = ["события:"]
    for e in rows:
        note = f" — {e['notes']}" if e["notes"] else ""
        lines.append(
            f"#{e['id']} — {_event_span(e['starts_ts'], e['ends_ts'], ctx.tz)} — {e['title']}{note}"
        )
    return "\n".join(lines)


async def _delete_event(args: dict, ctx: ToolContext) -> str:
    try:
        eid = int(args.get("id"))
    except (TypeError, ValueError):
        return "ошибка: нужен номер события"
    event = db.get_event(ctx.uid, eid)
    if not event:
        return f"событие #{eid} не найдено"
    db.delete_event(ctx.uid, eid)
    return f"событие #{eid} удалено: {event['title']}"


# ---------- интернет ----------

class _HTMLText(HTMLParser):
    SKIP_TAGS = {"script", "style", "noscript", "svg", "template"}
    BLOCK_TAGS = {
        "p", "br", "div", "li", "tr", "h1", "h2", "h3", "h4", "h5", "h6",
        "section", "article", "header", "footer", "table",
    }

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.skip = 0
        self.parts: list[str] = []
        self.title_parts: list[str] = []
        self._in_title = False

    def handle_starttag(self, tag, attrs):
        if tag in self.SKIP_TAGS:
            self.skip += 1
        elif tag == "title":
            self._in_title = True
        elif tag in self.BLOCK_TAGS:
            self.parts.append("\n")

    def handle_startendtag(self, tag, attrs):
        if tag in self.BLOCK_TAGS:
            self.parts.append("\n")

    def handle_endtag(self, tag):
        if tag in self.SKIP_TAGS:
            if self.skip:
                self.skip -= 1
        elif tag == "title":
            self._in_title = False
        elif tag in self.BLOCK_TAGS:
            self.parts.append("\n")

    def handle_data(self, data):
        if self.skip:
            return
        if self._in_title:
            self.title_parts.append(data)
        else:
            text = data.strip()
            if text:
                self.parts.append(text + " ")

    @property
    def title(self) -> str:
        return " ".join("".join(self.title_parts).split())

    def text(self) -> str:
        raw = "".join(self.parts)
        raw = re.sub(r"[ \t]+", " ", raw)
        raw = re.sub(r"\n\s*\n+", "\n\n", raw)
        return raw.strip()


def _is_safe_url(url: str) -> bool:
    try:
        parts = urlsplit(url)
    except ValueError:
        return False
    if parts.scheme not in ("http", "https") or not parts.hostname:
        return False
    try:
        port = parts.port or (443 if parts.scheme == "https" else 80)
    except ValueError:
        return False
    if port not in (80, 443):
        return False
    host = parts.hostname.lower()
    if host == "localhost" or host.endswith(".local"):
        return False
    try:
        infos = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    except OSError:
        return False
    for info in infos:
        try:
            ip = ipaddress.ip_address(info[4][0])
        except ValueError:
            return False
        if (ip.is_private or ip.is_loopback or ip.is_link_local
                or ip.is_reserved or ip.is_multicast or ip.is_unspecified):
            return False
    return True


def _decode_html(data: bytes, ctype: str) -> str:
    charset = None
    match = re.search(r"charset=([\w-]+)", ctype or "", re.I)
    if match:
        charset = match.group(1)
    if not charset:
        match = re.search(rb"<meta[^>]+charset=[\"']?([\w-]+)", data[:4096], re.I)
        if match:
            charset = match.group(1).decode("ascii", "ignore")
    for encoding in (charset, "utf-8", "cp1251"):
        if not encoding:
            continue
        try:
            return data.decode(encoding)
        except (UnicodeDecodeError, LookupError):
            continue
    return data.decode("utf-8", errors="ignore")


def _ddgs_search(query: str, limit: int):
    from ddgs import DDGS
    return DDGS().text(query, max_results=limit)


async def _tavily_search(query: str, limit: int) -> list[dict]:
    payload = {
        "api_key": TAVILY_API_KEY,
        "query": query,
        "max_results": limit,
        "search_depth": "basic",
        "include_answer": False,
    }
    connector = aiohttp.TCPConnector(ssl=ssl.create_default_context(cafile=certifi.where()))
    timeout = aiohttp.ClientTimeout(total=25)
    async with aiohttp.ClientSession(connector=connector, timeout=timeout) as session:
        async with session.post("https://api.tavily.com/search", json=payload) as resp:
            if resp.status != 200:
                raise RuntimeError(f"tavily ответил {resp.status}")
            data = await resp.json()
    return [
        {
            "title": (r.get("title") or "").strip(),
            "url": (r.get("url") or "").strip(),
            "snippet": (r.get("content") or "").strip(),
        }
        for r in data.get("results", [])
        if r.get("url")
    ][:limit]


async def _search_web(query: str, limit: int) -> list[dict]:
    if TAVILY_API_KEY:
        return await _tavily_search(query, limit)
    rows = await asyncio.wait_for(
        asyncio.to_thread(_ddgs_search, query, limit), timeout=30
    )
    results = []
    for row in rows or []:
        if not isinstance(row, dict):
            continue
        url = (row.get("href") or row.get("url") or "").strip()
        if not url:
            continue
        results.append({
            "title": (row.get("title") or "").strip(),
            "url": url,
            "snippet": (row.get("body") or row.get("snippet") or "").strip(),
        })
    return results[:limit]


async def _web_search(args: dict, ctx: ToolContext) -> str:
    query = str(args.get("query", "")).strip()
    if not query:
        return "ошибка: пустой поисковый запрос"
    try:
        limit = int(args.get("max_results", 5))
    except (TypeError, ValueError):
        limit = 5
    limit = max(1, min(limit, 10))
    try:
        results = await _search_web(query, limit)
    except Exception as e:
        return f"ошибка поиска: {e}"
    if not results:
        return "ничего не нашла по этому запросу"
    parts = []
    for i, r in enumerate(results, 1):
        parts.append(f"{i}. {r['title']}\n{r['url']}\n{r['snippet']}")
    return "\n\n".join(parts)


async def _fetch_page(args: dict, ctx: ToolContext) -> str:
    url = str(args.get("url", "")).strip()
    if not url:
        return "ошибка: пустая ссылка"
    if not url.startswith(("http://", "https://")):
        url = "https://" + url
    headers = {
        "User-Agent": USER_AGENT,
        "Accept-Language": "ru-RU,ru;q=0.9,en;q=0.8",
    }
    connector = aiohttp.TCPConnector(ssl=ssl.create_default_context(cafile=certifi.where()))
    timeout = aiohttp.ClientTimeout(total=25)
    data = b""
    ctype = ""
    final_url = url
    async with aiohttp.ClientSession(connector=connector, timeout=timeout, headers=headers) as session:
        for _ in range(4):
            if not await asyncio.to_thread(_is_safe_url, final_url):
                return "ошибка: недопустимая или небезопасная ссылка"
            async with session.get(final_url, allow_redirects=False) as resp:
                if resp.status in (301, 302, 303, 307, 308):
                    location = resp.headers.get("Location")
                    if not location:
                        return "ошибка: редирект без адреса"
                    final_url = urljoin(final_url, location)
                    continue
                if resp.status >= 400:
                    return f"ошибка: сайт ответил {resp.status}"
                ctype = resp.headers.get("Content-Type", "")
                data = await resp.content.read(2_000_000)
                break
        else:
            return "ошибка: слишком много редиректов"

    if "application/pdf" in ctype.lower():
        try:
            from pypdf import PdfReader
            reader = PdfReader(BytesIO(data))
            text = "\n".join((page.extract_text() or "") for page in reader.pages)
        except Exception as e:
            return f"не смогла прочитать pdf: {e}"
        title = ""
    elif "html" in ctype.lower() or not ctype:
        parser = _HTMLText()
        try:
            parser.feed(_decode_html(data, ctype))
        except Exception:
            pass
        title, text = parser.title, parser.text()
    else:
        text = data.decode("utf-8", errors="ignore")
        title = ""

    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n\s*\n+", "\n\n", text).strip()
    if not text:
        return "на странице нет читаемого текста"
    head = f"url: {final_url}\n" + (f"заголовок: {title}\n" if title else "")
    return head + "\n" + text[:8000]


async def _remember(args: dict, ctx: ToolContext) -> str:
    fact = str(args.get("fact", "")).strip()
    if not fact:
        return "ошибка: пустой факт"
    fid = db.add_fact(ctx.uid, fact)
    if fid is None:
        return "этот факт уже сохранён"
    return f"запомнила факт #{fid}: {fact}"


async def _list_facts(args: dict, ctx: ToolContext) -> str:
    rows = db.list_facts(ctx.uid)
    if not rows:
        return "пока ничего не помню о пользователе"
    return "что я помню:\n" + "\n".join(f"#{f['id']} — {f['text']}" for f in rows)


async def _forget(args: dict, ctx: ToolContext) -> str:
    try:
        fid = int(args.get("id"))
    except (TypeError, ValueError):
        return "ошибка: нужен номер факта"
    if db.delete_fact(ctx.uid, fid):
        return f"факт #{fid} удалён из памяти"
    return f"факт #{fid} не найден"


async def _request_voice_reply(args: dict, ctx: ToolContext) -> str:
    ctx.voice_requested = True
    return "ответ на это сообщение будет озвучен женским голосом"


async def _get_weather(args: dict, ctx: ToolContext) -> str:
    city = str(args.get("city", "")).strip() or ctx.city
    try:
        geo = await _get_json(
            "https://geocoding-api.open-meteo.com/v1/search",
            {"name": city, "count": 1, "language": "ru", "format": "json"},
        )
        results = geo.get("results") or []
        if not results:
            return f"не нашла город «{city}»"
        place = results[0]
        forecast = await _get_json(
            "https://api.open-meteo.com/v1/forecast",
            {
                "latitude": place["latitude"],
                "longitude": place["longitude"],
                "current": "temperature_2m,apparent_temperature,weather_code,wind_speed_10m",
                "daily": "weather_code,temperature_2m_max,temperature_2m_min,precipitation_probability_max",
                "timezone": "auto",
                "forecast_days": 2,
            },
        )
    except Exception as e:
        return f"ошибка погоды: {e}"

    current = forecast.get("current") or {}
    daily = forecast.get("daily") or {}
    tmax = daily.get("temperature_2m_max") or []
    tmin = daily.get("temperature_2m_min") or []
    codes = daily.get("weather_code") or []
    precip = daily.get("precipitation_probability_max") or []

    def desc(code):
        return WEATHER_CODES.get(code, "неизвестно")

    name = place.get("name", city)
    temp = current.get("temperature_2m")
    feels = current.get("apparent_temperature")
    wind = current.get("wind_speed_10m")
    lines = [
        f"погода: {name}",
        f"сейчас {round(temp)}°, ощущается {round(feels)}°, {desc(current.get('weather_code'))}, ветер {round(wind)} м/с"
        if temp is not None and feels is not None and wind is not None
        else "текущие данные недоступны",
    ]
    if len(tmax) > 0 and len(tmin) > 0:
        lines.append(f"сегодня {round(tmin[0])}…{round(tmax[0])}°, {desc(codes[0] if codes else None)}, осадки {precip[0] if precip else 0}%")
    if len(tmax) > 1 and len(tmin) > 1:
        lines.append(f"завтра {round(tmin[1])}…{round(tmax[1])}°, {desc(codes[1] if len(codes) > 1 else None)}, осадки {precip[1] if len(precip) > 1 else 0}%")
    return "\n".join(lines)


async def _get_schedule(args: dict, ctx: ToolContext) -> str:
    raw = str(args.get("day", "")).strip().lower()
    now = datetime.now(ctx.tz)
    if raw in ("", "сегодня", "today"):
        day = WEEKDAYS[now.weekday()]
    elif raw in ("завтра", "tomorrow"):
        day = WEEKDAYS[(now + timedelta(days=1)).weekday()]
    elif raw in WEEKDAYS:
        day = raw
    else:
        return f"неизвестный день «{raw}», нужно: {', '.join(WEEKDAYS)}"
    text = db.get_day(day)
    if not text.strip():
        return f"на {day} расписание не задано"
    return f"расписание на {day}:\n{text}"


async def _get_current_datetime(args: dict, ctx: ToolContext) -> str:
    now = datetime.now(ctx.tz)
    return f"{now.day} {MONTHS[now.month - 1]} {now.year}, {WEEKDAYS[now.weekday()]}, {now.hour:02d}:{now.minute:02d} ({ctx.tz.key})"


HANDLERS = {
    "create_reminder": _create_reminder,
    "list_reminders": _list_reminders,
    "cancel_reminder": _cancel_reminder,
    "add_todo": _add_todo,
    "list_todos": _list_todos,
    "complete_todo": _complete_todo,
    "add_event": _add_event,
    "list_events": _list_events,
    "delete_event": _delete_event,
    "web_search": _web_search,
    "fetch_page": _fetch_page,
    "remember": _remember,
    "list_facts": _list_facts,
    "forget": _forget,
    "request_voice_reply": _request_voice_reply,
    "get_weather": _get_weather,
    "get_schedule": _get_schedule,
    "get_current_datetime": _get_current_datetime,
}


async def execute_tool(name: str, args: dict, ctx: ToolContext) -> str:
    handler = HANDLERS.get(name)
    if handler is None:
        return f"ошибка: неизвестный инструмент «{name}»"
    if not isinstance(args, dict):
        return "ошибка: аргументы инструмента должны быть объектом"
    try:
        return await handler(args, ctx)
    except Exception as e:
        return f"ошибка инструмента {name}: {e}"
