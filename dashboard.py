"""Веб-дашборд: страница входа, index.html и JSON API. Вход по логину и паролю, сессия 8 часов."""
import asyncio
import hashlib
import hmac
import logging
import secrets
import time
from datetime import datetime, timezone
from pathlib import Path
import re
from urllib.parse import unquote, urlparse
from zoneinfo import ZoneInfo

from aiohttp import web

import config
import db
import validators
from menu_config import ACCOUNTANT_CATEGORIES, CATEGORIES, SETTINGS, STATUS_TITLES

log = logging.getLogger("dashboard")
BASE = Path(__file__).parent
TZ = ZoneInfo(config.BOT_TZ)

COOKIE = "hrsid"
MAX_FAILS = 5
LOCK_SECONDS = 15 * 60
_fails: dict[str, dict] = {}

# Действия, которые передаёт main.py (ответ, статус, рассылка)
_actions = None

SECURITY_HEADERS = {
    "Cache-Control": "no-store",
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
    "Referrer-Policy": "no-referrer",
    "Content-Security-Policy": (
        "default-src 'none'; script-src 'unsafe-inline'; style-src 'unsafe-inline'; "
        "connect-src 'self'; img-src data:; base-uri 'none'; form-action 'self'"
    ),
}


# ---------------- время ----------------
def local(iso: str | None) -> str | None:
    if not iso:
        return None
    dt = datetime.strptime(iso, db.FMT).replace(tzinfo=timezone.utc).astimezone(TZ)
    return dt.strftime("%d.%m.%Y %H:%M")


# ---------------- сессии ----------------
def _key() -> bytes:
    return hashlib.sha256(f"session|{config.DASHBOARD_USER}|{config.DASHBOARD_PASSWORD}".encode()).digest()


def _sign(msg: str) -> str:
    return hmac.new(_key(), msg.encode(), "sha256").hexdigest()


def make_session(lifetime_seconds: float | None = None) -> tuple[str, int]:
    """Подписанный токен сессии. Срок жизни считается от входа и не продлевается."""
    exp = int(time.time() + (config.SESSION_HOURS * 3600 if lifetime_seconds is None else lifetime_seconds))
    msg = f"{exp}.{secrets.token_hex(8)}"
    return f"{msg}.{_sign(msg)}", exp


def session_exp(token: str | None) -> int | None:
    """Время окончания сессии, если токен подлинный и срок не истёк."""
    if not token:
        return None
    try:
        exp_s, nonce, sig = token.split(".")
        if not hmac.compare_digest(sig, _sign(f"{exp_s}.{nonce}")):
            return None
        exp = int(exp_s)
    except ValueError:
        return None
    return exp if exp > time.time() else None


def is_https(request: web.Request) -> bool:
    return request.secure or request.headers.get("X-Forwarded-Proto", "").lower() == "https"


def client_ip(request: web.Request) -> str:
    fwd = request.headers.get("X-Forwarded-For", "").split(",")[0].strip()
    return fwd or request.remote or "?"


def origin_ok(request: web.Request) -> bool:
    """Защита от подделки запросов с чужих сайтов: нужен свой заголовок и совпадающий адрес."""
    if request.headers.get("X-Dashboard") != "1":
        return False
    origin = request.headers.get("Origin")
    if not origin:
        return True
    allowed = {request.host, request.headers.get("X-Forwarded-Host", "").split(",")[0].strip()}
    return urlparse(origin).netloc in allowed


def bad(message: str, status: int = 400) -> web.Response:
    return web.json_response({"ok": False, "error": message}, status=status)


@web.middleware
async def guard(request: web.Request, handler):
    path = request.path
    if path == "/health":
        return web.Response(text="ok")
    if request.method == "POST" and not origin_ok(request):
        return web.Response(status=403, text="Запрос отклонён", headers=SECURITY_HEADERS)
    if path != "/login":
        exp = session_exp(request.cookies.get(COOKIE))
        if exp is None:
            if path.startswith("/api/") or request.method != "GET":
                return web.json_response({"ok": False, "error": "auth"}, status=401, headers=SECURITY_HEADERS)
            raise web.HTTPFound("/login")
        request["session_exp"] = exp
    response = await handler(request)
    for k, v in SECURITY_HEADERS.items():
        response.headers[k] = v
    return response


# ---------------- вход и выход ----------------
async def login_page(request: web.Request):
    if session_exp(request.cookies.get(COOKIE)):
        raise web.HTTPFound("/")
    return web.Response(text=(BASE / "login.html").read_text(encoding="utf-8"), content_type="text/html")


async def login_post(request: web.Request):
    ip = client_ip(request)
    rec = _fails.get(ip)
    if rec and rec["until"] > time.time():
        minutes = int((rec["until"] - time.time()) // 60) + 1
        return bad(f"Слишком много неверных попыток. Повторите через {minutes} мин.", 429)
    data = await _json_body(request)
    user = str(data.get("user", "")).strip()
    password = str(data.get("password", ""))
    ok = (hmac.compare_digest(user.encode(), config.DASHBOARD_USER.encode())
          & hmac.compare_digest(password.encode(), config.DASHBOARD_PASSWORD.encode()))
    if not ok:
        await asyncio.sleep(1)  # замедляет подбор пароля
        rec = _fails.setdefault(ip, {"n": 0, "until": 0})
        rec["n"] += 1
        if rec["n"] >= MAX_FAILS:
            rec.update(n=0, until=time.time() + LOCK_SECONDS)
            log.warning("Дашборд: блокировка IP %s после неверных паролей", ip)
        return bad("Неверный логин или пароль.", 401)
    _fails.pop(ip, None)
    token, _exp = make_session()
    resp = web.json_response({"ok": True})
    resp.set_cookie(COOKIE, token, max_age=int(config.SESSION_HOURS * 3600), httponly=True,
                    samesite="Strict", secure=is_https(request), path="/")
    return resp


async def logout_post(request: web.Request):
    resp = web.json_response({"ok": True})
    resp.del_cookie(COOKIE, path="/")
    return resp


# ---------------- вспомогательное ----------------
async def _json_body(request: web.Request) -> dict:
    try:
        data = await request.json()
    except Exception:
        raise web.HTTPBadRequest(text="bad json")
    if not isinstance(data, dict):
        raise web.HTTPBadRequest(text="bad json")
    return data


async def read_form(request: web.Request) -> tuple[dict, list[tuple[str, bytes]]]:
    """Поля формы и файлы (multipart) либо JSON без файлов."""
    if request.content_type == "application/json":
        return {k: v for k, v in (await _json_body(request)).items() if isinstance(v, str)}, []
    post = await request.post()
    fields = {k: v for k, v in post.items() if isinstance(v, str)}
    files = []
    for f in post.getall("files", []):
        if hasattr(f, "filename") and f.filename:
            name = f.filename
            if re.search(r"%[0-9A-Fa-f]{2}", name):  # имя могло прийти в процентной кодировке
                name = unquote(name)
            files.append((name, f.file.read()))
    return fields, files


def _id(request: web.Request, name: str = "id") -> int:
    try:
        return int(request.match_info[name])
    except ValueError:
        raise web.HTTPBadRequest(text="bad id")


def serialize(r: dict, users: dict | None = None) -> dict:
    now_s = db.now_iso()
    active = r["status"] in db.ACTIVE
    u = (users or {}).get(r["user_id"]) or db.get_user(r["user_id"]) or {}
    return {
        "id": r["id"],
        "category": r["category"],
        "category_title": CATEGORIES.get(r["category"], {}).get("title", r["category"]),
        "status": r["status"],
        "status_title": STATUS_TITLES.get(r["status"], r["status"]),
        "user_id": r["user_id"],
        "user_name": r["user_name"],
        "user_full_name": u.get("full_name"),
        "user_position": u.get("position"),
        "user_note": u.get("note"),
        "created_local": local(r["created_at"]),
        "due_local": local(r["due_at"]),
        "closed_local": local(r["closed_at"]),
        "overdue": bool(active and r["due_at"] and r["due_at"] < now_s),
        "fields": r["fields"],
    }


# ---------------- страницы и статистика ----------------
async def index(request: web.Request):
    return web.Response(text=(BASE / "index.html").read_text(encoding="utf-8"), content_type="text/html")


async def api_stats(request: web.Request):
    s = db.stats(config.BOT_TZ)
    cats = []
    for key, cat in CATEGORIES.items():
        c = s["by_category"].get(key, {})
        cats.append({
            "key": key, "title": cat["title"], "sla_hours": cat["sla_hours"],
            "accountant": key in ACCOUNTANT_CATEGORIES,
            "new": c.get("new", 0), "in_work": c.get("in_work", 0),
            "done": c.get("done", 0), "rejected": c.get("rejected", 0),
            "overdue": c.get("overdue", 0),
        })
    exp = request.get("session_exp")
    return web.json_response({
        "generated_local": datetime.now(TZ).strftime("%d.%m.%Y %H:%M"),
        "timezone": config.BOT_TZ,
        "session_until": datetime.fromtimestamp(exp, TZ).strftime("%H:%M") if exp else None,
        "users_total": s["users_total"],
        "users_consent": s["users_consent"],
        "users_week": s["users_week"],
        "requests_total": s["requests_total"],
        "by_status": s["by_status"],
        "overdue": s["overdue"],
        "avg_close_hours": s["avg_close_hours"],
        "categories": cats,
        "daily": s["daily"],
        "settings": [{"key": k, "title": t, "value": db.get_setting(k)} for k, t in SETTINGS.items()],
        "limits": {"max_files": config.MAX_FILES, "max_mb": config.MAX_FILE_MB,
                   "extensions": sorted(config.ALLOWED_EXT)},
        "recipients": db.count_users(),
        "broadcast_running": bool(_actions and _actions.broadcast_running()),
    })


# ---------------- заявки ----------------
async def api_requests(request: web.Request):
    qs = request.query
    try:
        limit = max(1, min(int(qs.get("limit", 50)), 200))
        offset = max(0, int(qs.get("offset", 0)))
    except ValueError:
        raise web.HTTPBadRequest(text="bad paging")
    category = qs.get("category", "")
    if any(c not in CATEGORIES for c in category.split(",") if c):
        raise web.HTTPBadRequest(text="bad category")
    status = qs.get("status", "")
    if status not in ("", "active", "overdue", *STATUS_TITLES):
        raise web.HTTPBadRequest(text="bad status")
    items, total = db.search_requests(category, status, qs.get("q", "")[:100], limit, offset)
    users = db.users_by_ids(list({r["user_id"] for r in items}))
    return web.json_response({"total": total, "items": [serialize(r, users) for r in items]})


async def api_request(request: web.Request):
    req = db.get_request(_id(request))
    if not req:
        raise web.HTTPNotFound(text="not found")
    data = serialize(req)
    data["replies"] = [
        {"created_local": local(r["created_at"]), "text": r["text"], "from_id": r["from_id"], "files": r["files"]}
        for r in db.get_replies(req["id"])
    ]
    return web.json_response(data)


async def api_reply(request: web.Request):
    if _actions is None:
        raise web.HTTPServiceUnavailable(text="Отправка недоступна")
    req_id = _id(request)
    fields, files = await read_form(request)
    text = fields.get("text", "").strip()
    if len(text) > 1500:
        return bad("Слишком длинный ответ (не более 1500 символов).")
    ok, cleaned = validators.clean_files(files)
    if not ok:
        return bad(cleaned)
    if not text and not cleaned:
        return bad("Напишите текст ответа или приложите файл.")
    delivered = await _actions.reply(req_id, text, cleaned)
    if delivered is None:
        raise web.HTTPNotFound(text="not found")
    log.info("Дашборд: ответ по заявке №%s (файлов: %s, доставлен: %s)", req_id, len(cleaned), delivered)
    return web.json_response({"ok": True, "delivered": bool(delivered)})


async def api_status(request: web.Request):
    if _actions is None:
        raise web.HTTPServiceUnavailable(text="Смена статуса недоступна")
    req_id = _id(request)
    status = (await _json_body(request)).get("status")
    if status not in STATUS_TITLES:
        raise web.HTTPBadRequest(text="bad status")
    if not await _actions.status(req_id, status):
        raise web.HTTPNotFound(text="not found")
    log.info("Дашборд: заявка №%s → %s", req_id, status)
    return web.json_response({"ok": True})


# ---------------- даты выплат ----------------
async def api_setting(request: web.Request):
    """Изменить дату выплаты, которую видят работники. Пустое значение сбрасывает дату."""
    key = request.match_info["key"]
    if key not in SETTINGS:
        raise web.HTTPNotFound(text="not found")
    value = str((await _json_body(request)).get("value", "")).strip()
    if not value:
        db.clear_setting(key)
        log.info("Дашборд: дата «%s» сброшена", key)
        return web.json_response({"ok": True, "value": ""})
    ok, result = validators.check("pay_date", value)
    if not ok:
        return bad(result)
    db.set_setting(key, result)
    log.info("Дашборд: дата «%s» = %s", key, result)
    return web.json_response({"ok": True, "value": result})


# ---------------- контакты ----------------
def serialize_user(u: dict) -> dict:
    return {
        "user_id": u["user_id"], "name": u["name"], "full_name": u.get("full_name") or "",
        "position": u.get("position") or "", "note": u.get("note") or "",
        "consent": bool(u["consent"]), "requests": u.get("requests", 0),
        "created_local": local(u["created_at"]), "last_seen_local": local(u.get("last_seen")),
    }


async def api_contacts(request: web.Request):
    qs = request.query
    try:
        limit = max(1, min(int(qs.get("limit", 100)), 500))
        offset = max(0, int(qs.get("offset", 0)))
    except ValueError:
        raise web.HTTPBadRequest(text="bad paging")
    users, total = db.list_users(qs.get("q", "")[:100], limit, offset)
    return web.json_response({"total": total, "items": [serialize_user(u) for u in users]})


async def api_contact_update(request: web.Request):
    uid = _id(request, "uid")
    data = await _json_body(request)
    if not db.get_user(uid):
        raise web.HTTPNotFound(text="not found")
    for field, limit in db.CONTACT_FIELDS.items():
        if field in data:
            value = str(data[field]).strip()
            if len(value) > limit:
                return bad(f"Поле слишком длинное (не более {limit} символов).")
            db.update_contact(uid, field, value)
    log.info("Дашборд: обновлён контакт %s", uid)
    users, _ = db.list_users(str(uid), 1)
    return web.json_response({"ok": True, "item": serialize_user(users[0])})


# ---------------- сообщение всем подписчикам ----------------
async def api_broadcasts(request: web.Request):
    items = [{
        "id": b["id"], "created_local": local(b["created_at"]), "text": b["text"], "files": b["files"],
        "total": b["total"], "ok": b["ok"], "failed": b["failed"], "status": b["status"],
    } for b in db.list_broadcasts(20)]
    return web.json_response({"items": items, "recipients": db.count_users(),
                              "running": bool(_actions and _actions.broadcast_running())})


async def api_broadcast_send(request: web.Request):
    if _actions is None:
        raise web.HTTPServiceUnavailable(text="Рассылка недоступна")
    fields, files = await read_form(request)
    text = fields.get("text", "").strip()
    if len(text) > 3500:
        return bad("Слишком длинное сообщение (не более 3500 символов).")
    ok, cleaned = validators.clean_files(files)
    if not ok:
        return bad(cleaned)
    if not text and not cleaned:
        return bad("Напишите текст сообщения или приложите файл.")
    try:
        bc_id = await _actions.broadcast(text, cleaned)
    except ValueError as e:
        if str(e) == "busy":
            return bad("Предыдущая рассылка ещё идёт. Дождитесь её завершения.", 409)
        return bad("Пока нет подписчиков: никто ещё не запускал бота.")
    log.info("Дашборд: запущена рассылка №%s (файлов: %s)", bc_id, len(cleaned))
    return web.json_response({"ok": True, "id": bc_id})


def make_app() -> web.Application:
    limit = (config.MAX_FILES * config.MAX_FILE_MB + 5) * 1024 * 1024
    app = web.Application(middlewares=[guard], client_max_size=limit)
    r = app.router
    r.add_get("/", index)
    r.add_get("/health", index)  # обрабатывается в guard
    r.add_get("/login", login_page)
    r.add_post("/login", login_post)
    r.add_post("/logout", logout_post)
    r.add_get("/api/stats", api_stats)
    r.add_get("/api/requests", api_requests)
    r.add_get("/api/request/{id}", api_request)
    r.add_post("/api/request/{id}/reply", api_reply)
    r.add_post("/api/request/{id}/status", api_status)
    r.add_post("/api/settings/{key}", api_setting)
    r.add_get("/api/contacts", api_contacts)
    r.add_post("/api/contacts/{uid}", api_contact_update)
    r.add_get("/api/broadcasts", api_broadcasts)
    r.add_post("/api/broadcast", api_broadcast_send)
    return app


async def start(actions=None):
    """Запускает дашборд, если задан пароль. Ошибки дашборда не должны останавливать бота."""
    global _actions
    _actions = actions
    if not config.DASHBOARD_PASSWORD:
        log.info("Дашборд выключен: не задана переменная DASHBOARD_PASSWORD")
        return None
    if len(config.DASHBOARD_PASSWORD) < 8:
        log.warning("Дашборд выключен: пароль DASHBOARD_PASSWORD короче 8 символов")
        return None
    try:
        runner = web.AppRunner(make_app())
        await runner.setup()
        await web.TCPSite(runner, "0.0.0.0", config.DASHBOARD_PORT).start()
        log.info("Дашборд запущен на порту %s (логин: %s, сессия %s ч)",
                 config.DASHBOARD_PORT, config.DASHBOARD_USER, config.SESSION_HOURS)
        return runner
    except Exception:
        log.exception("Не удалось запустить дашборд (бот продолжает работать)")
        return None
