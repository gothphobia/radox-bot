"""планировщик: напоминания и фоновые задачи, переживающие рестарт."""

import time
from datetime import datetime
from zoneinfo import ZoneInfo

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.date import DateTrigger

import db

TZ = ZoneInfo("Europe/Moscow")
REPEAT_STEP = {"daily": 86400, "weekly": 604800}

aps = AsyncIOScheduler(timezone=TZ)
_bot = None


def init(bot):
    global _bot
    _bot = bot


def start():
    if not aps.running:
        aps.start()


def shutdown():
    if aps.running:
        aps.shutdown(wait=False)


def _job_id(rid: int) -> str:
    return f"reminder:{rid}"


def _next_ts(due_ts: int, repeat: str, now: float | None = None) -> int:
    now = time.time() if now is None else now
    step = REPEAT_STEP.get(repeat, 0)
    if not step:
        return due_ts
    while due_ts <= now:
        due_ts += step
    return due_ts


def schedule_reminder(reminder: dict):
    """поставить (или переставить) job для напоминания из БД."""
    run_date = datetime.fromtimestamp(reminder["due_ts"], TZ)
    aps.add_job(
        fire_reminder,
        trigger=DateTrigger(run_date=run_date, timezone=TZ),
        args=[reminder["id"]],
        id=_job_id(reminder["id"]),
        replace_existing=True,
        misfire_grace_time=3600,
        coalesce=True,
    )


def cancel_reminder_job(rid: int):
    try:
        aps.remove_job(_job_id(rid))
    except Exception:
        pass


async def fire_reminder(rid: int):
    reminder = db.get_reminder(rid)
    if not reminder:
        return
    repeat = reminder["repeat"]
    text = reminder["text"]
    uid = reminder["uid"]

    if repeat in REPEAT_STEP:
        next_ts = _next_ts(reminder["due_ts"], repeat, time.time())
        db.update_reminder_due(rid, next_ts)
        schedule_reminder({**reminder, "due_ts": next_ts})
    else:
        db.delete_reminder(rid)

    if _bot is None:
        return
    try:
        await _bot.send_message(uid, f"напоминание: {text}")
    except Exception as e:
        print("reminder error", e)


def restore_reminders():
    """поднять все напоминания из БД после рестарта."""
    now = time.time()
    for reminder in db.all_pending_reminders():
        due = reminder["due_ts"]
        if due <= now:
            if reminder["repeat"] in REPEAT_STEP:
                due = _next_ts(due, reminder["repeat"], now)
                db.update_reminder_due(reminder["id"], due)
            elif now - due > 86400:
                # слишком старое разовое напоминание — не спамим
                db.delete_reminder(reminder["id"])
                continue
            else:
                due = now + 3
                db.update_reminder_due(reminder["id"], due)
        schedule_reminder({**reminder, "due_ts": due})
