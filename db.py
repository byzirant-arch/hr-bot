"""Хранилище: SQLite. Все заявки, пользователи, даты выплат и состояние диалогов."""
import json
import sqlite3
import threading
from datetime import datetime, timedelta, timezone

import config

FMT = "%Y-%m-%dT%H:%M:%S"  # время хранится в UTC
ACTIVE = ("new", "in_work")
CLOSED = ("done", "rejected")

_lock = threading.Lock()
_conn: sqlite3.Connection | None = None


def now_iso() -> str:
    return datetime.now(timezone.utc).strftime(FMT)


def _db() -> sqlite3.Connection:
    global _conn
    if _conn is None:
        _conn = sqlite3.connect(config.DB_PATH, check_same_thread=False)
        _conn.row_factory = sqlite3.Row
    return _conn


def _run(sql: str, params: tuple = (), fetch: str | None = None):
    with _lock:
        cur = _db().execute(sql, params)
        _db().commit()
        if fetch == "one":
            row = cur.fetchone()
            return dict(row) if row else None
        if fetch == "all":
            return [dict(r) for r in cur.fetchall()]
        if fetch == "id":
            return cur.lastrowid
        return None


def init() -> None:
    with _lock:
        _db().executescript(
            """
            CREATE TABLE IF NOT EXISTS users(
                user_id INTEGER PRIMARY KEY,
                name TEXT,
                consent INTEGER NOT NULL DEFAULT 0,
                created_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS settings(
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS requests(
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                created_at TEXT NOT NULL,
                due_at TEXT,
                user_id INTEGER NOT NULL,
                user_name TEXT,
                category TEXT NOT NULL,
                fields TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'new',
                reminder_sent INTEGER NOT NULL DEFAULT 0,
                closed_at TEXT
            );
            CREATE INDEX IF NOT EXISTS idx_requests_cat ON requests(category, status);
            CREATE TABLE IF NOT EXISTS replies(
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                request_id INTEGER NOT NULL,
                from_id INTEGER NOT NULL,
                text TEXT NOT NULL,
                created_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS sessions(
                user_id INTEGER PRIMARY KEY,
                state TEXT NOT NULL,
                data TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
            """
        )
        _db().commit()


# ---------- пользователи ----------
def upsert_user(user_id: int, name: str) -> None:
    _run(
        "INSERT INTO users(user_id, name, created_at) VALUES(?,?,?) "
        "ON CONFLICT(user_id) DO UPDATE SET name=excluded.name",
        (user_id, name, now_iso()),
    )


def has_consent(user_id: int) -> bool:
    row = _run("SELECT consent FROM users WHERE user_id=?", (user_id,), "one")
    return bool(row and row["consent"])


def set_consent(user_id: int) -> None:
    _run("UPDATE users SET consent=1 WHERE user_id=?", (user_id,))


# ---------- настройки (даты выплат) ----------
def get_setting(key: str) -> str | None:
    row = _run("SELECT value FROM settings WHERE key=?", (key,), "one")
    return row["value"] if row else None


def set_setting(key: str, value: str) -> None:
    _run(
        "INSERT INTO settings(key, value, updated_at) VALUES(?,?,?) "
        "ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at",
        (key, value, now_iso()),
    )


# ---------- состояние диалога ----------
def get_session(user_id: int) -> dict | None:
    row = _run("SELECT state, data FROM sessions WHERE user_id=?", (user_id,), "one")
    if not row:
        return None
    return {"state": row["state"], "data": json.loads(row["data"])}


def set_session(user_id: int, state: str, data: dict) -> None:
    _run(
        "INSERT INTO sessions(user_id, state, data, updated_at) VALUES(?,?,?,?) "
        "ON CONFLICT(user_id) DO UPDATE SET state=excluded.state, data=excluded.data, "
        "updated_at=excluded.updated_at",
        (user_id, state, json.dumps(data, ensure_ascii=False), now_iso()),
    )


def clear_session(user_id: int) -> None:
    _run("DELETE FROM sessions WHERE user_id=?", (user_id,))


# ---------- заявки ----------
def create_request(user_id: int, user_name: str, category: str,
                   fields: list[list[str]], sla_hours: int | None) -> int:
    created = datetime.now(timezone.utc)
    due = (created + timedelta(hours=sla_hours)).strftime(FMT) if sla_hours else None
    return _run(
        "INSERT INTO requests(created_at, due_at, user_id, user_name, category, fields) "
        "VALUES(?,?,?,?,?,?)",
        (created.strftime(FMT), due, user_id, user_name, category,
         json.dumps(fields, ensure_ascii=False)),
        "id",
    )


def _decode(row: dict | None) -> dict | None:
    if row:
        row["fields"] = json.loads(row["fields"])
    return row


def get_request(request_id: int) -> dict | None:
    return _decode(_run("SELECT * FROM requests WHERE id=?", (request_id,), "one"))


def list_requests(category: str, closed: bool, limit: int = 10) -> list[dict]:
    statuses = CLOSED if closed else ACTIVE
    marks = ",".join("?" * len(statuses))
    rows = _run(
        f"SELECT * FROM requests WHERE category=? AND status IN ({marks}) "
        "ORDER BY id DESC LIMIT ?",
        (category, *statuses, limit),
        "all",
    )
    return [_decode(r) for r in rows]


def count_active_by_category() -> dict[str, int]:
    rows = _run(
        "SELECT category, COUNT(*) AS n FROM requests "
        "WHERE status IN ('new','in_work') GROUP BY category",
        (), "all",
    )
    return {r["category"]: r["n"] for r in rows}


def set_status(request_id: int, status: str) -> None:
    closed_at = now_iso() if status in CLOSED else None
    _run("UPDATE requests SET status=?, closed_at=? WHERE id=?", (status, closed_at, request_id))


def add_reply(request_id: int, from_id: int, text: str) -> None:
    _run(
        "INSERT INTO replies(request_id, from_id, text, created_at) VALUES(?,?,?,?)",
        (request_id, from_id, text, now_iso()),
    )


def get_replies(request_id: int) -> list[dict]:
    return _run(
        "SELECT * FROM replies WHERE request_id=? ORDER BY id", (request_id,), "all"
    )


def due_for_reminder(before_hours: float) -> list[dict]:
    limit = (datetime.now(timezone.utc) + timedelta(hours=before_hours)).strftime(FMT)
    rows = _run(
        "SELECT * FROM requests WHERE status IN ('new','in_work') AND due_at IS NOT NULL "
        "AND reminder_sent=0 AND due_at <= ?",
        (limit,), "all",
    )
    return [_decode(r) for r in rows]


def mark_reminded(request_id: int) -> None:
    _run("UPDATE requests SET reminder_sent=1 WHERE id=?", (request_id,))
