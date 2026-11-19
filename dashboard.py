"""Веб-дашборд: страница index.html + JSON API (только чтение), защита паролем (Basic Auth)."""
import base64
import hmac
import logging
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlparse
from zoneinfo import ZoneInfo

from aiohttp import web

import config
import db
import validators
from menu_config import CATEGORIES, SETTINGS, STATUS_TITLES

log = logging.getLogger("dashboard")
INDEX = Path(__file__).parent / "index.html"
TZ = ZoneInfo(config.BOT_TZ)

MAX_FAILS = 5
LOCK_SECONDS = 15 * 60
_fails: dict[str, dict] = {}

# Функции отправки ответа и смены статуса передаёт main.py (чтобы дашборд мог писать работнику через бота)
_reply_cb = None
_status_cb = None
MAX_REPLY = 1500

SECURITY_HEADERS = {
    "Cache-Control": "no-store",
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
    "Referrer-Policy": "no-referrer",
    "Content-Security-Policy": (
        "default-src 'none'; script-src 'unsafe-inline'; style-src 'unsafe-inline'; "
        "connect-src 'self'; img-src data:; base-uri 'none'; form-action 'none'"
    ),
}


def local(iso: str | None) -> str | None:
    if not iso:
        return None
    dt = datetime.strptime(iso, db.FMT).replace(tzinfo=timezone.utc).astimezone(TZ)
    return dt.strftime("%d.%m.%Y %H:%M")


def client_ip(request: web.Request) -> str:
    fwd = request.headers.get("X-Forwarded-For", "").split(",")[0].strip()
    return fwd or request.remote or "?"


def credentials_ok(header: str) -> bool:
    if not header.startswith("Basic "):
        return False
    try:
        user, _, pwd = base64.b64decode(header[6:]).decode("utf-8").partition(":")
    except Exception:
        return False
    ok_user = hmac.compare_digest(user.encode(), config.DASHBOARD_USER.encode())
    ok_pwd = hmac.compare_digest(pwd.encode(), config.DASHBOARD_PASSWORD.encode())
    return ok_user and ok_pwd


def origin_ok(request: web.Request) -> bool:
    """Защита от подделки запросов с чужих сайтов: нужен свой заголовок и совпадающий адрес."""
    if request.headers.get("X-Dashboard") != "1":
        return False
    origin = request.headers.get("Origin")
    if not origin:
        return True
    allowed = {request.host, request.headers.get("X-Forwarded-Host", "").split(",")[0].strip()}
    return urlparse(origin).netloc in allowed


@web.middleware
async def guard(request: web.Request, handler):
    if request.path == "/health":
        return web.Response(text="ok")
    ip = client_ip(request)
    rec = _fails.get(ip)
    if rec and rec["until"] > time.time():
        return web.Response(status=429, text="Слишком много неверных попыток. Повторите через 15 минут.",
                            headers=SECURITY_HEADERS)
    header = request.headers.get("Authorization", "")
    if not credentials_ok(header):
        if header:  # введён неверный пароль (а не первый запрос без пароля)
            rec = _fails.setdefault(ip, {"n": 0, "until": 0})
            rec["n"] += 1
            if rec["n"] >= MAX_FAILS:
                rec.update(n=0, until=time.time() + LOCK_SECONDS)
                log.warning("Дашборд: блокировка IP %s после неверных паролей", ip)
        return web.Response(status=401, text="Нужен логин и пароль",
                            headers={**SECURITY_HEADERS,
                                     "WWW-Authenticate": 'Basic realm="Панель бота", charset="UTF-8"'})
    _fails.pop(ip, None)
    if request.method == "POST" and not origin_ok(request):
        return web.Response(status=403, text="Запрос отклонён", headers=SECURITY_HEADERS)
    response = await handler(request)
    for k, v in SECURITY_HEADERS.items():
        response.headers[k] = v
    return response


def serialize(r: dict) -> dict:
    now_s = db.now_iso()
    active = r["status"] in db.ACTIVE
    return {
        "id": r["id"],
        "category": r["category"],
        "category_title": CATEGORIES.get(r["category"], {}).get("title", r["category"]),
        "status": r["status"],
        "status_title": STATUS_TITLES.get(r["status"], r["status"]),
        "user_id": r["user_id"],
        "user_name": r["user_name"],
        "created_local": local(r["created_at"]),
        "due_local": local(r["due_at"]),
        "closed_local": local(r["closed_at"]),
        "overdue": bool(active and r["due_at"] and r["due_at"] < now_s),
        "fields": r["fields"],
    }


async def index(request: web.Request):
    return web.Response(text=INDEX.read_text(encoding="utf-8"), content_type="text/html")


async def api_stats(request: web.Request):
    s = db.stats(config.BOT_TZ)
    cats = []
    for key, cat in CATEGORIES.items():
        c = s["by_category"].get(key, {})
        cats.append({
            "key": key, "title": cat["title"], "sla_hours": cat["sla_hours"],
            "new": c.get("new", 0), "in_work": c.get("in_work", 0),
            "done": c.get("done", 0), "rejected": c.get("rejected", 0),
            "overdue": c.get("overdue", 0),
        })
    return web.json_response({
        "generated_local": datetime.now(TZ).strftime("%d.%m.%Y %H:%M"),
        "timezone": config.BOT_TZ,
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
        "staff": {"admins": len(config.ADMIN_IDS), "responsible": len(config.RESPONSIBLE_IDS),
                  "remind_before_hours": config.REMIND_BEFORE_HOURS},
    })


async def api_requests(request: web.Request):
    qs = request.query
    try:
        limit = max(1, min(int(qs.get("limit", 50)), 200))
        offset = max(0, int(qs.get("offset", 0)))
    except ValueError:
        raise web.HTTPBadRequest(text="bad paging")
    category = qs.get("category", "")
    if category and category not in CATEGORIES:
        raise web.HTTPBadRequest(text="bad category")
    status = qs.get("status", "")
    if status not in ("", "active", "overdue", *STATUS_TITLES):
        raise web.HTTPBadRequest(text="bad status")
    items, total = db.search_requests(category, status, qs.get("q", "")[:100], limit, offset)
    return web.json_response({"total": total, "items": [serialize(r) for r in items]})


async def api_request(request: web.Request):
    try:
        req_id = int(request.match_info["id"])
    except ValueError:
        raise web.HTTPBadRequest(text="bad id")
    req = db.get_request(req_id)
    if not req:
        raise web.HTTPNotFound(text="not found")
    data = serialize(req)
    data["replies"] = [
        {"created_local": local(r["created_at"]), "text": r["text"], "from_id": r["from_id"]}
        for r in db.get_replies(req_id)
    ]
    return web.json_response(data)


async def _json_body(request: web.Request) -> dict:
    try:
        data = await request.json()
    except Exception:
        raise web.HTTPBadRequest(text="bad json")
    if not isinstance(data, dict):
        raise web.HTTPBadRequest(text="bad json")
    return data


def _id(request: web.Request) -> int:
    try:
        return int(request.match_info["id"])
    except ValueError:
        raise web.HTTPBadRequest(text="bad id")


async def api_reply(request: web.Request):
    if _reply_cb is None:
        raise web.HTTPServiceUnavailable(text="Отправка недоступна")
    req_id = _id(request)
    text = str((await _json_body(request)).get("text", "")).strip()
    if not text or len(text) > MAX_REPLY:
        raise web.HTTPBadRequest(text="bad text")
    delivered = await _reply_cb(req_id, text)
    if delivered is None:
        raise web.HTTPNotFound(text="not found")
    log.info("Дашборд: ответ по заявке №%s (доставлен: %s)", req_id, delivered)
    return web.json_response({"ok": True, "delivered": bool(delivered)})


async def api_status(request: web.Request):
    if _status_cb is None:
        raise web.HTTPServiceUnavailable(text="Смена статуса недоступна")
    req_id = _id(request)
    status = (await _json_body(request)).get("status")
    if status not in STATUS_TITLES:
        raise web.HTTPBadRequest(text="bad status")
    if not await _status_cb(req_id, status):
        raise web.HTTPNotFound(text="not found")
    log.info("Дашборд: заявка №%s → %s", req_id, status)
    return web.json_response({"ok": True})


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
        return web.json_response({"ok": False, "error": result}, status=400)
    db.set_setting(key, result)
    log.info("Дашборд: дата «%s» = %s", key, result)
    return web.json_response({"ok": True, "value": result})


def make_app() -> web.Application:
    app = web.Application(middlewares=[guard])
    app.router.add_get("/", index)
    app.router.add_get("/health", index)  # обрабатывается в guard
    app.router.add_get("/api/stats", api_stats)
    app.router.add_get("/api/requests", api_requests)
    app.router.add_get("/api/request/{id}", api_request)
    app.router.add_post("/api/request/{id}/reply", api_reply)
    app.router.add_post("/api/request/{id}/status", api_status)
    app.router.add_post("/api/settings/{key}", api_setting)
    return app


async def start(reply_cb=None, status_cb=None):
    """Запускает дашборд, если задан пароль. Ошибки дашборда не должны останавливать бота."""
    global _reply_cb, _status_cb
    _reply_cb, _status_cb = reply_cb, status_cb
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
        log.info("Дашборд запущен на порту %s (логин: %s)", config.DASHBOARD_PORT, config.DASHBOARD_USER)
        return runner
    except Exception:
        log.exception("Не удалось запустить дашборд (бот продолжает работать)")
        return None
