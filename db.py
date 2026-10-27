"""постоянное хранилище: история, напоминания, дела, расписание, настройки."""

import json
import sqlite3
import threading
import time
from pathlib import Path

DB_PATH = Path(__file__).parent / "bot.db"

_lock = threading.RLock()
_conn = sqlite3.connect(DB_PATH, check_same_thread=False)
_conn.row_factory = sqlite3.Row
_conn.execute("PRAGMA journal_mode=WAL")
_conn.execute("PRAGMA synchronous=NORMAL")

USER_FIELDS = {"model", "effort", "city", "tools", "voice"}
REPEAT_VALUES = ("none", "daily", "weekly")


def _ensure_column(table: str, column: str, ddl: str):
    columns = {row[1] for row in _conn.execute(f"PRAGMA table_info({table})")}
    if column not in columns:
        _conn.execute(f"ALTER TABLE {table} ADD COLUMN {ddl}")


def init(default_schedule: dict[str, str] | None = None):
    with _lock:
        _conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS users (
                uid INTEGER PRIMARY KEY,
                model TEXT,
                effort TEXT NOT NULL DEFAULT 'none',
                city TEXT NOT NULL DEFAULT 'Севастополь',
                tools INTEGER NOT NULL DEFAULT 1,
                voice INTEGER NOT NULL DEFAULT 0,
                created_ts INTEGER NOT NULL
            );

            CREATE TABLE IF NOT EXISTS messages (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                uid INTEGER NOT NULL,
                role TEXT NOT NULL,
                content TEXT NOT NULL,
                created_ts INTEGER NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_messages_uid ON messages(uid, id);

            CREATE TABLE IF NOT EXISTS reminders (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                uid INTEGER NOT NULL,
                text TEXT NOT NULL,
                due_ts INTEGER NOT NULL,
                repeat TEXT NOT NULL DEFAULT 'none',
                created_ts INTEGER NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_reminders_due ON reminders(due_ts);

            CREATE TABLE IF NOT EXISTS todos (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                uid INTEGER NOT NULL,
                text TEXT NOT NULL,
                done INTEGER NOT NULL DEFAULT 0,
                created_ts INTEGER NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_todos_uid ON todos(uid, done);

            CREATE TABLE IF NOT EXISTS schedule (
                day TEXT PRIMARY KEY,
                text TEXT NOT NULL DEFAULT ''
            );

            CREATE TABLE IF NOT EXISTS settings (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS facts (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                uid INTEGER NOT NULL,
                text TEXT NOT NULL,
                norm TEXT NOT NULL,
                created_ts INTEGER NOT NULL
            );
            CREATE UNIQUE INDEX IF NOT EXISTS idx_facts_norm ON facts(uid, norm);

            CREATE TABLE IF NOT EXISTS summaries (
                uid INTEGER PRIMARY KEY,
                text TEXT NOT NULL,
                up_to_id INTEGER NOT NULL DEFAULT 0,
                updated_ts INTEGER NOT NULL
            );

            CREATE TABLE IF NOT EXISTS events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                uid INTEGER NOT NULL,
                title TEXT NOT NULL,
                starts_ts INTEGER NOT NULL,
                ends_ts INTEGER NOT NULL,
                notes TEXT NOT NULL DEFAULT '',
                created_ts INTEGER NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_events_uid_start ON events(uid, starts_ts);
            """
        )
        # миграции для уже существующих баз
        _ensure_column("users", "tools", "tools INTEGER NOT NULL DEFAULT 1")
        _ensure_column("users", "voice", "voice INTEGER NOT NULL DEFAULT 0")
        for day, text in (default_schedule or {}).items():
            _conn.execute(
                "INSERT OR IGNORE INTO schedule(day, text) VALUES(?, ?)", (day, text)
            )
        _conn.execute(
            "INSERT OR IGNORE INTO settings(key, value) VALUES('school_enabled', '1')"
        )
        _conn.commit()


# ---------- пользователи ----------

def get_user(uid: int) -> dict:
    with _lock:
        row = _conn.execute("SELECT * FROM users WHERE uid = ?", (uid,)).fetchone()
        if row is None:
            _conn.execute(
                "INSERT INTO users(uid, created_ts) VALUES(?, ?)",
                (uid, int(time.time())),
            )
            _conn.commit()
            row = _conn.execute("SELECT * FROM users WHERE uid = ?", (uid,)).fetchone()
        return dict(row)


def set_user(uid: int, **fields):
    bad = set(fields) - USER_FIELDS
    if bad:
        raise ValueError(f"неизвестные поля пользователя: {bad}")
    if not fields:
        return
    columns = ", ".join(f"{name} = ?" for name in fields)
    values = [int(v) if isinstance(v, bool) else v for v in fields.values()]
    with _lock:
        _conn.execute("INSERT OR IGNORE INTO users(uid, created_ts) VALUES(?, ?)", (uid, int(time.time())))
        _conn.execute(f"UPDATE users SET {columns} WHERE uid = ?", (*values, uid))
        _conn.commit()


def all_user_ids() -> list[int]:
    with _lock:
        rows = _conn.execute("SELECT uid FROM users ORDER BY uid").fetchall()
    return [r["uid"] for r in rows]


# ---------- история ----------

def append_message(uid: int, role: str, content):
    payload = json.dumps(content, ensure_ascii=False)
    with _lock:
        _conn.execute(
            "INSERT INTO messages(uid, role, content, created_ts) VALUES(?, ?, ?, ?)",
            (uid, role, payload, int(time.time())),
        )
        _conn.commit()


def get_history(uid: int, limit: int = 200) -> list[dict]:
    with _lock:
        rows = _conn.execute(
            "SELECT role, content FROM messages WHERE uid = ? ORDER BY id DESC LIMIT ?",
            (uid, limit),
        ).fetchall()
    return [
        {"role": r["role"], "content": json.loads(r["content"])}
        for r in reversed(rows)
    ]


def clear_history(uid: int):
    with _lock:
        _conn.execute("DELETE FROM messages WHERE uid = ?", (uid,))
        _conn.execute("DELETE FROM summaries WHERE uid = ?", (uid,))
        _conn.commit()


def _message_row(row) -> dict:
    return {
        "id": row["id"],
        "role": row["role"],
        "content": json.loads(row["content"]),
    }


def get_messages_after(uid: int, after_id: int, limit: int = 200) -> list[dict]:
    with _lock:
        rows = _conn.execute(
            "SELECT id, role, content FROM messages "
            "WHERE uid = ? AND id > ? ORDER BY id LIMIT ?",
            (uid, after_id, limit),
        ).fetchall()
    return [_message_row(r) for r in rows]


def get_messages_range(uid: int, after_id: int, up_to_id: int, limit: int = 500) -> list[dict]:
    with _lock:
        rows = _conn.execute(
            "SELECT id, role, content FROM messages "
            "WHERE uid = ? AND id > ? AND id <= ? ORDER BY id LIMIT ?",
            (uid, after_id, up_to_id, limit),
        ).fetchall()
    return [_message_row(r) for r in rows]


def count_messages_after(uid: int, after_id: int) -> int:
    with _lock:
        row = _conn.execute(
            "SELECT COUNT(*) AS n FROM messages WHERE uid = ? AND id > ?",
            (uid, after_id),
        ).fetchone()
    return row["n"]


def max_message_id(uid: int) -> int:
    with _lock:
        row = _conn.execute(
            "SELECT MAX(id) AS m FROM messages WHERE uid = ?", (uid,)
        ).fetchone()
    return row["m"] or 0


def get_context(uid: int, limit: int = 60) -> list[dict]:
    """последние сообщения после уже сжатой части диалога."""
    summary = get_summary(uid)
    after_id = summary["up_to_id"] if summary else 0
    with _lock:
        rows = _conn.execute(
            "SELECT role, content FROM messages "
            "WHERE uid = ? AND id > ? ORDER BY id DESC LIMIT ?",
            (uid, after_id, limit),
        ).fetchall()
    return [
        {"role": r["role"], "content": json.loads(r["content"])}
        for r in reversed(rows)
    ]


# ---------- память ----------

def _normalize_fact(text: str) -> str:
    return " ".join(str(text).split()).strip().lower()


def add_fact(uid: int, text: str) -> int | None:
    text = " ".join(str(text).split()).strip()[:500]
    if not text:
        return None
    norm = _normalize_fact(text)
    with _lock:
        cur = _conn.execute(
            "INSERT OR IGNORE INTO facts(uid, text, norm, created_ts) VALUES(?, ?, ?, ?)",
            (uid, text, norm, int(time.time())),
        )
        _conn.commit()
        return cur.lastrowid if cur.rowcount else None


def list_facts(uid: int) -> list[dict]:
    with _lock:
        rows = _conn.execute(
            "SELECT * FROM facts WHERE uid = ? ORDER BY id", (uid,)
        ).fetchall()
    return [dict(r) for r in rows]


def delete_fact(uid: int, fid: int) -> bool:
    with _lock:
        cur = _conn.execute(
            "DELETE FROM facts WHERE id = ? AND uid = ?", (fid, uid)
        )
        _conn.commit()
        return cur.rowcount > 0


def clear_facts(uid: int):
    with _lock:
        _conn.execute("DELETE FROM facts WHERE uid = ?", (uid,))
        _conn.commit()


def get_summary(uid: int) -> dict | None:
    with _lock:
        row = _conn.execute(
            "SELECT * FROM summaries WHERE uid = ?", (uid,)
        ).fetchone()
    return dict(row) if row else None


def set_summary(uid: int, text: str, up_to_id: int):
    with _lock:
        _conn.execute(
            "INSERT INTO summaries(uid, text, up_to_id, updated_ts) VALUES(?, ?, ?, ?) "
            "ON CONFLICT(uid) DO UPDATE SET text = excluded.text, "
            "up_to_id = excluded.up_to_id, updated_ts = excluded.updated_ts",
            (uid, text, up_to_id, int(time.time())),
        )
        _conn.commit()


def clear_summary(uid: int):
    with _lock:
        _conn.execute("DELETE FROM summaries WHERE uid = ?", (uid,))
        _conn.commit()


# ---------- календарь ----------

def add_event(uid: int, title: str, starts_ts: int, ends_ts: int, notes: str = "") -> int:
    with _lock:
        cur = _conn.execute(
            "INSERT INTO events(uid, title, starts_ts, ends_ts, notes, created_ts) "
            "VALUES(?, ?, ?, ?, ?, ?)",
            (uid, title, starts_ts, ends_ts, notes, int(time.time())),
        )
        _conn.commit()
        return cur.lastrowid


def get_event(uid: int, eid: int) -> dict | None:
    with _lock:
        row = _conn.execute(
            "SELECT * FROM events WHERE id = ? AND uid = ?", (eid, uid)
        ).fetchone()
    return dict(row) if row else None


def list_events(uid: int, from_ts: int, to_ts: int) -> list[dict]:
    """события, пересекающиеся с диапазоном [from_ts, to_ts)."""
    with _lock:
        rows = _conn.execute(
            "SELECT * FROM events WHERE uid = ? AND starts_ts < ? AND ends_ts > ? "
            "ORDER BY starts_ts",
            (uid, to_ts, from_ts),
        ).fetchall()
    return [dict(r) for r in rows]


def delete_event(uid: int, eid: int) -> bool:
    with _lock:
        cur = _conn.execute(
            "DELETE FROM events WHERE id = ? AND uid = ?", (eid, uid)
        )
        _conn.commit()
        return cur.rowcount > 0


# ---------- напоминания ----------

def add_reminder(uid: int, text: str, due_ts: int, repeat: str = "none") -> int:
    if repeat not in REPEAT_VALUES:
        repeat = "none"
    with _lock:
        cur = _conn.execute(
            "INSERT INTO reminders(uid, text, due_ts, repeat, created_ts) VALUES(?, ?, ?, ?, ?)",
            (uid, text, due_ts, repeat, int(time.time())),
        )
        _conn.commit()
        return cur.lastrowid


def get_reminder(rid: int) -> dict | None:
    with _lock:
        row = _conn.execute("SELECT * FROM reminders WHERE id = ?", (rid,)).fetchone()
    return dict(row) if row else None


def list_reminders(uid: int) -> list[dict]:
    with _lock:
        rows = _conn.execute(
            "SELECT * FROM reminders WHERE uid = ? ORDER BY due_ts", (uid,)
        ).fetchall()
    return [dict(r) for r in rows]


def all_pending_reminders() -> list[dict]:
    with _lock:
        rows = _conn.execute("SELECT * FROM reminders ORDER BY due_ts").fetchall()
    return [dict(r) for r in rows]


def update_reminder_due(rid: int, due_ts: int):
    with _lock:
        _conn.execute("UPDATE reminders SET due_ts = ? WHERE id = ?", (due_ts, rid))
        _conn.commit()


def delete_reminder(rid: int):
    with _lock:
        _conn.execute("DELETE FROM reminders WHERE id = ?", (rid,))
        _conn.commit()


# ---------- дела ----------

def add_todo(uid: int, text: str) -> int:
    with _lock:
        cur = _conn.execute(
            "INSERT INTO todos(uid, text, created_ts) VALUES(?, ?, ?)",
            (uid, text, int(time.time())),
        )
        _conn.commit()
        return cur.lastrowid


def list_todos(uid: int, include_done: bool = False) -> list[dict]:
    query = "SELECT * FROM todos WHERE uid = ?"
    params: list = [uid]
    if not include_done:
        query += " AND done = 0"
    query += " ORDER BY done, id"
    with _lock:
        rows = _conn.execute(query, params).fetchall()
    return [dict(r) for r in rows]


def complete_todo(uid: int, tid: int) -> bool:
    with _lock:
        cur = _conn.execute(
            "UPDATE todos SET done = 1 WHERE id = ? AND uid = ?", (tid, uid)
        )
        _conn.commit()
        return cur.rowcount > 0


# ---------- расписание ----------

def get_schedule() -> dict[str, str]:
    with _lock:
        rows = _conn.execute("SELECT day, text FROM schedule").fetchall()
    return {r["day"]: r["text"] for r in rows}


def get_day(day: str) -> str:
    with _lock:
        row = _conn.execute("SELECT text FROM schedule WHERE day = ?", (day,)).fetchone()
    return row["text"] if row else ""


def set_schedule(day: str, text: str):
    with _lock:
        _conn.execute(
            "INSERT INTO schedule(day, text) VALUES(?, ?) "
            "ON CONFLICT(day) DO UPDATE SET text = excluded.text",
            (day, text),
        )
        _conn.commit()


# ---------- настройки ----------

def get_setting(key: str, default: str | None = None) -> str | None:
    with _lock:
        row = _conn.execute("SELECT value FROM settings WHERE key = ?", (key,)).fetchone()
    return row["value"] if row else default


def set_setting(key: str, value: str):
    with _lock:
        _conn.execute(
            "INSERT INTO settings(key, value) VALUES(?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (key, value),
        )
        _conn.commit()
