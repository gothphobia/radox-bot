"""утренний брифинг: погода, школа, напоминания и дела на сегодня."""

from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import db
from tools import ToolContext, execute_tool

TZ = ZoneInfo("Europe/Moscow")
WEEKDAYS = ["понедельник", "вторник", "среда", "четверг", "пятница", "суббота", "воскресенье"]
MONTHS = ["января", "февраля", "марта", "апреля", "мая", "июня", "июля", "августа", "сентября", "октября", "ноября", "декабря"]


async def build(uid: int, now: datetime | None = None, weather_cache: dict | None = None) -> str:
    now = now or datetime.now(TZ)
    user = db.get_user(uid)
    cache = weather_cache if weather_cache is not None else {}

    lines = [
        "доброе утро!! ^^",
        f"сегодня {now.day} {MONTHS[now.month - 1]}, {WEEKDAYS[now.weekday()]}",
    ]

    city = user["city"]
    if city not in cache:
        try:
            cache[city] = await execute_tool(
                "get_weather", {}, ToolContext(uid=uid, tz=TZ, city=city)
            )
        except Exception as e:
            cache[city] = f"погода недоступна: {e}"
    lines += ["", cache[city]]

    if now.weekday() < 5:
        rasp = db.get_day(WEEKDAYS[now.weekday()]) or ""
        if rasp.strip():
            lines += ["", "в школе:", rasp.strip()]

    start = datetime(now.year, now.month, now.day, tzinfo=TZ)
    end = start + timedelta(days=1)

    events = db.list_events(uid, int(start.timestamp()), int(end.timestamp()))
    if events:
        lines += ["", "события на сегодня:"]
        for e in events[:10]:
            event_start = datetime.fromtimestamp(e["starts_ts"], TZ)
            event_end = datetime.fromtimestamp(e["ends_ts"], TZ)
            if event_start.date() == start.date():
                span = f"{event_start:%H:%M}–{event_end:%H:%M}"
            else:
                span = f"с {event_start:%d.%m %H:%M} до {event_end:%d.%m %H:%M}"
            lines.append(f"• {span} — {e['title']}")

    reminders = [
        r for r in db.list_reminders(uid)
        if start.timestamp() <= r["due_ts"] < end.timestamp()
    ]
    if reminders:
        lines += ["", "напоминания на сегодня:"]
        for r in reminders[:10]:
            due = datetime.fromtimestamp(r["due_ts"], TZ)
            lines.append(f"• {due:%H:%M} — {r['text']}")

    todos = db.list_todos(uid)[:10]
    if todos:
        lines += ["", "дела:"]
        for t in todos:
            lines.append(f"• #{t['id']} — {t['text']}")

    lines += ["", "хорошего дня ^-^"]
    return "\n".join(lines)[:4000]
