"""долговременная память: факты о пользователе и конспект старых разговоров."""

import asyncio
import json

import db
import llm

SUMMARY_TRIGGER = 30      # сколько несжатых сообщений накапливать перед сжатием
SUMMARY_KEEP = 10         # последние сообщения всегда оставляем как есть
SUMMARY_MAX_CHARS = 1800
FACTS_BATCH = 8           # сообщений между извлечениями фактов
FACTS_KEEP_IN_PROMPT = 40
FACTS_MAX_IN_PROMPT = 1800

_locks: dict[int, asyncio.Lock] = {}


def _lock_for(uid: int) -> asyncio.Lock:
    lock = _locks.get(uid)
    if lock is None:
        lock = asyncio.Lock()
        _locks[uid] = lock
    return lock


def _content_text(content) -> str:
    if isinstance(content, str):
        return content
    parts = []
    for part in content or []:
        if isinstance(part, dict):
            if part.get("type") == "text":
                parts.append(part.get("text", ""))
            elif part.get("type") == "image_url":
                parts.append("[изображение]")
    return "\n".join(p for p in parts if p)


def _dialog_text(messages: list, limit: int = 8000) -> str:
    lines = []
    for m in messages:
        text = _content_text(m.get("content")).strip()
        if not text:
            continue
        role = "user" if m.get("role") == "user" else "assistant"
        lines.append(f"{role}: {text}")
    return "\n".join(lines)[-limit:]


def _parse_json_list(raw: str) -> list[str]:
    raw = (raw or "").strip()
    start, end = raw.find("["), raw.rfind("]")
    if start == -1 or end == -1 or end <= start:
        return []
    try:
        data = json.loads(raw[start:end + 1])
    except json.JSONDecodeError:
        return []
    if not isinstance(data, list):
        return []
    return [str(item).strip() for item in data if isinstance(item, (str, int, float)) and str(item).strip()]


async def _maybe_extract_facts(uid: int):
    marker_key = f"facts_upto:{uid}"
    try:
        marker = int(db.get_setting(marker_key, "0") or 0)
    except ValueError:
        marker = 0
    new_messages = db.get_messages_after(uid, marker, limit=50)
    if len(new_messages) < FACTS_BATCH:
        return

    batch = new_messages[-20:]
    known = db.list_facts(uid)
    known_text = "\n".join(f"- {f['text']}" for f in known[-50:]) or "(пока нет)"
    dialog = _dialog_text(batch)

    try:
        raw = await llm.complete(
            [
                {
                    "role": "system",
                    "content": (
                        "ты — модуль долговременной памяти личного ассистента. "
                        "выпиши только новые устойчивые факты о пользователе: имя, возраст или класс, "
                        "город, предпочтения, цели, важные обстоятельства, привычки, договорённости. "
                        "не выписывай мелочи, эмоции, содержание текущих задач, погоду и то, что уже известно. "
                        "каждый факт — короткая фраза от третьего лица до 120 символов. "
                        "ответ строго JSON-массивом строк, без пояснений. если нового нет — []."
                    ),
                },
                {
                    "role": "user",
                    "content": f"известные факты:\n{known_text}\n\nновые сообщения:\n{dialog}",
                },
            ],
            max_tokens=300,
        )
    except Exception as e:
        print("memory facts error", e)
        return

    for fact in _parse_json_list(raw)[:6]:
        db.add_fact(uid, fact[:300])
    db.set_setting(marker_key, str(batch[-1]["id"]))


async def _maybe_summarize(uid: int):
    summary = db.get_summary(uid)
    after_id = summary["up_to_id"] if summary else 0
    if db.count_messages_after(uid, after_id) <= SUMMARY_TRIGGER:
        return

    rows = db.get_messages_after(uid, after_id, limit=500)
    to_summarize = rows[:-SUMMARY_KEEP] if len(rows) > SUMMARY_KEEP else []
    if not to_summarize:
        return

    previous = summary["text"] if summary else "(пусто)"
    dialog = _dialog_text(to_summarize)
    try:
        new_summary = await llm.complete(
            [
                {
                    "role": "system",
                    "content": (
                        "ты сжимаешь историю диалога личного ассистента. "
                        "объедини предыдущий конспект с новой частью. сохрани факты, договорённости, "
                        "незакрытые задачи и важные решения. убери приветствия, болтовню и повторы. "
                        "пиши по-русски, от третьего лица, одним связным текстом. "
                        "ответ — только конспект, без заголовков и пояснений."
                    ),
                },
                {
                    "role": "user",
                    "content": f"предыдущий конспект:\n{previous}\n\nновая часть диалога:\n{dialog}",
                },
            ],
            max_tokens=700,
        )
    except Exception as e:
        print("memory summary error", e)
        return

    new_summary = (new_summary or "").strip()
    if not new_summary:
        return
    db.set_summary(uid, new_summary[:SUMMARY_MAX_CHARS], to_summarize[-1]["id"])


async def after_turn(uid: int):
    """фоновая задача после ответа: факты и сжатие истории."""
    async with _lock_for(uid):
        try:
            await _maybe_extract_facts(uid)
        except Exception as e:
            print("memory facts error", e)
        try:
            await _maybe_summarize(uid)
        except Exception as e:
            print("memory summary error", e)


def facts_for_prompt(uid: int) -> str:
    facts = db.list_facts(uid)
    if not facts:
        return ""
    lines = [f"- {f['text']}" for f in facts[-FACTS_KEEP_IN_PROMPT:]]
    return "\n".join(lines)[:FACTS_MAX_IN_PROMPT]


def summary_for_prompt(uid: int) -> str:
    summary = db.get_summary(uid)
    return summary["text"][:SUMMARY_MAX_CHARS] if summary else ""
