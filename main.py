"""Информационный бот для работников в мессенджере MAX (по ТЗ)."""
import asyncio
import logging
from datetime import datetime, timezone
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import aiohttp
from maxapi import Bot, Dispatcher
from maxapi.types import BotStarted, CallbackButton, MessageCallback, MessageCreated
from maxapi.types.input_media import InputMediaBuffer
from maxapi.utils.inline_keyboard import InlineKeyboardBuilder
from maxapi.utils.message import process_input_media

import config
import dashboard
import db
import validators
from menu_config import (ACCOUNTANT_CATEGORIES, CATEGORIES, DATE_NOT_SET, FLOWS, INFO,
                         INSTANT, SCREENS, SETTINGS, STATUS_TITLES)

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("hrbot")

dp = Dispatcher()
bot: Bot | None = None
TZ = ZoneInfo(config.BOT_TZ)

HOME_BTN = ("🏠 Главное меню", "go:main")
HOME_ROW = [HOME_BTN]

CONTACT_LABELS = {"full_name": "ФИО", "position": "Должность", "note": "Пометка"}


# ======================================================================
# Роли и права
# ======================================================================
def is_admin(uid: int) -> bool:
    return uid in config.ADMIN_IDS


def is_hr(uid: int) -> bool:
    return uid in config.RESPONSIBLE_IDS


def is_staff(uid: int) -> bool:
    return uid in config.STAFF_IDS


def notify_ids(category: str) -> set[int]:
    """Кому уходит уведомление о заявке. Расчётные листы: бухгалтеру (если не задан, то администраторам)."""
    if category in ACCOUNTANT_CATEGORIES:
        return set(config.ACCOUNTANT_IDS) or set(config.ADMIN_IDS)
    return set(config.RESPONSIBLE_IDS)


def can_see(uid: int, category: str) -> bool:
    if is_admin(uid):
        return True
    if category in ACCOUNTANT_CATEGORIES:
        return uid in config.ACCOUNTANT_IDS
    return is_hr(uid)


def visible_categories(uid: int) -> list[str]:
    return [k for k in CATEGORIES if can_see(uid, k)]


def can_broadcast(uid: int) -> bool:
    return is_admin(uid) or is_hr(uid)


can_edit_contacts = can_broadcast


# ======================================================================
# Вспомогательные функции
# ======================================================================
def display_name(user) -> str:
    parts = [getattr(user, "first_name", None), getattr(user, "last_name", None)]
    name = " ".join(p for p in parts if p).strip()
    return name or getattr(user, "username", None) or f"ID {user.user_id}"


def keyboard(rows):
    builder = InlineKeyboardBuilder()
    for row in rows:
        builder.row(*[CallbackButton(text=t, payload=p) for t, p in row])
    return [builder.as_markup()]


async def send(uid: int, text: str | None, rows=None, media=None, fallback: bool = True) -> bool:
    """Отправить сообщение (с кнопками и файлами) по ID. Тексты заявок в лог не пишем."""
    attempts = [(rows, media)]
    if fallback and rows and media:  # если Max не принял кнопки вместе с файлом, шлём без кнопок
        attempts.append((None, media))
    for r, m in attempts:
        atts = (keyboard(r) if r else []) + list(m or [])
        try:
            result = await bot.send_message(user_id=uid, text=text or None, attachments=atts or None)
            if result.__class__.__name__ == "Error":
                log.warning("Max не принял сообщение для %s: %s", uid, getattr(result, "message", result))
                continue
            return True
        except Exception:
            log.exception("Не удалось отправить сообщение пользователю %s", uid)
    return False


def fmt_dt(iso: str | None) -> str:
    if not iso:
        return "—"
    dt = datetime.strptime(iso, db.FMT).replace(tzinfo=timezone.utc).astimezone(TZ)
    return dt.strftime("%d.%m.%Y %H:%M")


def request_card(req: dict, replies: list[dict] | None = None) -> str:
    cat = CATEGORIES.get(req["category"], {"title": req["category"]})
    lines = [
        f"Заявка №{req['id']}",
        f"Категория: {cat['title']}",
        f"Создана: {fmt_dt(req['created_at'])}",
        f"Работник: {req['user_name']} (ID в Max: {req['user_id']})",
    ]
    contact = db.get_user(req["user_id"]) or {}
    for key, label in CONTACT_LABELS.items():
        if contact.get(key):
            lines.append(f"{label} (контакты): {contact[key]}")
    lines.append(f"Статус: {STATUS_TITLES.get(req['status'], req['status'])}")
    if req.get("due_at"):
        lines.append(f"Срок ответа: до {fmt_dt(req['due_at'])}")
    lines.append("Содержание:")
    for label, value in req["fields"]:
        lines.append(f"• {label}: {value}")
    if replies:
        lines.append("Ответы работнику:")
        for r in replies[-3:]:
            files = f" [файлы: {', '.join(r['files'])}]" if r.get("files") else ""
            lines.append(f"• {fmt_dt(r['created_at'])}: {r['text']}{files}")
    return "\n".join(lines)


# ---------- вложения от сотрудников ----------
def extract_attachments(message) -> list[dict]:
    out = []
    body = getattr(message, "body", None)
    for a in (getattr(body, "attachments", None) or []):
        kind = getattr(a, "type", None)
        kind = getattr(kind, "value", kind)
        url = getattr(getattr(a, "payload", None), "url", None)
        if kind == "file":
            out.append({"kind": "file", "url": url, "filename": getattr(a, "filename", None) or "file",
                        "size": getattr(a, "size", None)})
        elif kind == "image":
            out.append({"kind": "image", "url": url, "filename": "photo.jpg", "size": None})
    return out


async def download_files(atts: list[dict]) -> list[tuple[str, bytes]]:
    """Скачивает файлы, которые прислал сотрудник, чтобы переслать их работнику."""
    limit = config.MAX_FILE_MB * 1024 * 1024
    files = []
    timeout = aiohttp.ClientTimeout(total=120)
    async with aiohttp.ClientSession(timeout=timeout) as session:
        for a in atts:
            name = validators.safe_filename(a["filename"])
            if not a.get("url"):
                raise ValueError(f"Файл «{name}» недоступен. Отправьте его ещё раз.")
            if a.get("size") and a["size"] > limit:
                raise ValueError(f"Файл «{name}» больше {config.MAX_FILE_MB} МБ.")
            async with session.get(a["url"]) as resp:
                if resp.status != 200:
                    raise ValueError(f"Не удалось получить файл «{name}». Отправьте его ещё раз.")
                data = await resp.content.read(limit + 1)
            files.append((name, data))
    ok, result = validators.clean_files(files)
    if not ok:
        raise ValueError(result)
    return result


def to_media(files: list[tuple[str, bytes]]) -> list:
    return [InputMediaBuffer(data, name) for name, data in files]


# ======================================================================
# Меню работника
# ======================================================================
def screen_rows(screen_id: str, uid: int):
    sc = SCREENS[screen_id]
    rows = [[(t, p)] for t, p in sc["buttons"]]
    if screen_id == "main":
        if is_staff(uid):
            rows.append([("🛠 Панель сотрудника", "adm:home")])
        return rows
    nav = []
    if sc["parent"]:
        nav.append(("← Назад", f"go:{sc['parent']}"))
    nav.append(HOME_BTN)
    rows.append(nav)
    return rows


async def show_screen(uid: int, screen_id: str):
    db.clear_session(uid)
    await send(uid, SCREENS[screen_id]["text"], screen_rows(screen_id, uid))


def back_rows(screen_id: str):
    return [[("← Назад", f"go:{screen_id}"), HOME_BTN]]


def flow_rows(flow_id: str):
    flow = FLOWS[flow_id]
    return [[("↺ Начать заново", f"flow:{flow_id}")],
            [("← Назад", f"go:{flow['screen']}"), HOME_BTN]]


async def ensure_consent(uid: int, name: str) -> bool:
    db.upsert_user(uid, name)
    if is_staff(uid) or db.has_consent(uid):
        return True
    await send(uid, config.CONSENT_TEXT, [[("✅ Согласен(на)", "consent:yes")]])
    return False


async def start_flow(uid: int, flow_id: str):
    flow = FLOWS[flow_id]
    db.set_session(uid, "flow", {"flow": flow_id, "step": 0, "answers": {}})
    await send(uid, flow["steps"][0]["prompt"], flow_rows(flow_id))


async def make_request(uid: int, name: str, category: str, fields: list[list[str]]) -> int:
    req_id = db.create_request(uid, name, category, fields, CATEGORIES[category]["sla_hours"])
    await notify_new_request(req_id)
    return req_id


async def notify_new_request(req_id: int):
    req = db.get_request(req_id)
    text = "🔔 Новая заявка\n\n" + request_card(req)
    ids = notify_ids(req["category"])
    if not ids:
        log.warning("Некому отправить уведомление о заявке №%s (категория %s)", req_id, req["category"])
    for rid in ids:
        await send(rid, text, [[("Открыть заявку", f"adm:req:{req_id}")]])


async def flow_answer(uid: int, name: str, sess: dict, text: str):
    data = sess["data"]
    flow_id = data["flow"]
    flow = FLOWS[flow_id]
    step = flow["steps"][data["step"]]
    ok, value = validators.check(step["validator"], text)
    if not ok:
        await send(uid, f"{value}\n\n{step['prompt']}", flow_rows(flow_id))
        return
    data["answers"][step["key"]] = value
    if data["step"] + 1 < len(flow["steps"]):
        data["step"] += 1
        db.set_session(uid, "flow", data)
        await send(uid, flow["steps"][data["step"]]["prompt"], flow_rows(flow_id))
        return
    fields = [[s["label"], data["answers"][s["key"]]] for s in flow["steps"]]
    db.clear_session(uid)
    await make_request(uid, name, flow["category"], fields)
    await send(uid, flow["done_text"].format(**data["answers"]), [HOME_ROW])


# ======================================================================
# Панель сотрудника
# ======================================================================
def admin_home_rows(uid: int):
    rows = [[("📥 Заявки", "adm:cats")], [("📊 Статистика", "adm:stats")]]
    if can_broadcast(uid):
        rows.append([("📢 Сообщение всем подписчикам", "adm:bc")])
    if can_edit_contacts(uid):
        rows.append([("👥 Контакты", "adm:ct")])
    if is_admin(uid):
        rows.append([("📅 Даты выплат", "adm:dates")])
    rows.append([HOME_BTN])
    return rows


async def adm_home(uid: int):
    db.clear_session(uid)
    _drafts.pop(uid, None)
    await send(uid, "Панель сотрудника. Выберите действие:", admin_home_rows(uid))


async def deny(uid: int):
    await send(uid, "Нет доступа.", [[("← Панель", "adm:home")]])


async def adm_cats(uid: int):
    counts = db.count_active_by_category()
    rows = []
    for key in visible_categories(uid):
        rows.append([(f"{CATEGORIES[key]['title']} ({counts.get(key, 0)})", f"adm:list:{key}:a")])
    rows.append([("← Назад", "adm:home")])
    await send(uid, "Заявки по категориям (в скобках — активные: новые и в работе):", rows)


async def adm_list(uid: int, cat: str, mode: str):
    if cat not in CATEGORIES or not can_see(uid, cat):
        return await deny(uid)
    closed = mode == "c"
    items = db.list_requests(cat, closed)
    rows = []
    for r in items:
        short = r["user_name"][:25]
        rows.append([(f"№{r['id']} · {short} · {STATUS_TITLES[r['status']]}", f"adm:req:{r['id']}")])
    rows.append([("📁 Архив" if not closed else "📂 Активные", f"adm:list:{cat}:{'c' if not closed else 'a'}")])
    rows.append([("← К категориям", "adm:cats")])
    head = f"{CATEGORIES[cat]['title']} — {'архив (исполненные и отклонённые)' if closed else 'активные заявки'}"
    if not items:
        head += "\n\nЗаявок нет."
    await send(uid, head, rows)


async def adm_request(uid: int, req_id: int):
    req = db.get_request(req_id)
    if not req:
        await send(uid, "Заявка не найдена.", [[("← К категориям", "adm:cats")]])
        return
    if not can_see(uid, req["category"]):
        return await deny(uid)
    active = req["status"] in db.ACTIVE
    rows = []
    if active:
        rows.append([("▶️ В работу", f"adm:st:{req_id}:in_work"), ("✅ Исполнена", f"adm:st:{req_id}:done")])
        rows.append([("❌ Отклонить", f"adm:st:{req_id}:rejected"), ("💬 Ответить", f"adm:reply:{req_id}")])
    else:
        rows.append([("🔄 Вернуть в работу", f"adm:st:{req_id}:in_work"), ("💬 Ответить", f"adm:reply:{req_id}")])
    rows.append([("← К списку", f"adm:list:{req['category']}:{'a' if active else 'c'}")])
    await send(uid, request_card(req, db.get_replies(req_id)), rows)


async def adm_status(uid: int, req_id: int, status: str):
    req = db.get_request(req_id)
    if not req or status not in STATUS_TITLES:
        return
    if not can_see(uid, req["category"]):
        return await deny(uid)
    db.set_status(req_id, status)
    await send(req["user_id"], f"Статус вашей заявки №{req_id} изменён: {STATUS_TITLES[status]}.", [HOME_ROW])
    await adm_request(uid, req_id)


# ---------- ответ работнику (текст, смайлики, файлы) ----------
async def adm_reply_start(uid: int, req_id: int):
    req = db.get_request(req_id)
    if not req:
        return
    if not can_see(uid, req["category"]):
        return await deny(uid)
    db.set_session(uid, "admin_reply", {"request_id": req_id})
    await send(
        uid,
        f"Ответ работнику по заявке №{req_id}.\n\nНапишите текст (можно со смайликами) и при желании "
        f"приложите файл или фото. Каждое сообщение уходит работнику сразу. "
        f"Когда закончите, нажмите «К заявке».",
        [[("← К заявке", f"adm:req:{req_id}")]],
    )


async def adm_reply_send(uid: int, sess: dict, text: str, atts: list[dict]):
    req_id = sess["data"]["request_id"]
    req = db.get_request(req_id)
    text = (text or "").strip()
    back = [[("← К заявке", f"adm:req:{req_id}")]]
    if not req or not can_see(uid, req["category"]):
        return await deny(uid)
    if not text and not atts:
        await send(uid, "Отправьте текст ответа или файл.", back)
        return
    if len(text) > 1500:
        await send(uid, "Слишком длинный ответ (не более 1500 символов). Сократите.", back)
        return
    try:
        files = await download_files(atts) if atts else []
    except ValueError as e:
        await send(uid, f"⚠️ {e}", back)
        return
    body = f"Ответ по вашей заявке №{req_id}:\n\n{text}" if text else f"Ответ по вашей заявке №{req_id}: вложение"
    delivered = await send(req["user_id"], body, [HOME_ROW], to_media(files))
    db.add_reply(req_id, uid, text, [n for n, _ in files])
    if delivered:
        await send(uid, "✅ Отправлено работнику. Можно написать ещё или вернуться к заявке.", back)
    else:
        await send(uid, "⚠️ Ответ сохранён, но доставить его работнику не удалось "
                        "(возможно, он ещё не запускал бота).", back)


# ---------- даты выплат ----------
def dates_text() -> str:
    return "\n".join(f"{title}: {db.get_setting(key) or DATE_NOT_SET}" for key, title in SETTINGS.items())


async def adm_dates(uid: int):
    db.clear_session(uid)
    rows = [[(f"Изменить: {title}", f"adm:setdate:{key}")] for key, title in SETTINGS.items()]
    rows.append([("← Назад", "adm:home")])
    await send(uid, "Текущие даты выплат:\n\n" + dates_text(), rows)


async def adm_setdate_start(uid: int, key: str):
    if key not in SETTINGS:
        return
    db.set_session(uid, "admin_setdate", {"key": key})
    await send(uid, f"{SETTINGS[key]}.\nВведите новую дату в формате ДД.ММ.ГГГГ (например, 25.12.2026).",
               [[("Отмена", "adm:dates")]])


async def adm_setdate_save(uid: int, sess: dict, text: str):
    key = sess["data"]["key"]
    ok, value = validators.check("pay_date", text)
    if not ok:
        await send(uid, f"{value}\n\n{SETTINGS[key]}. Введите дату в формате ДД.ММ.ГГГГ.",
                   [[("Отмена", "adm:dates")]])
        return
    db.set_setting(key, value)
    db.clear_session(uid)
    await send(uid, f"✅ Готово. {SETTINGS[key]}: {value}.\nРаботники сразу увидят её в ответах бота.",
               [[("📅 Даты выплат", "adm:dates")], [("← Панель", "adm:home")]])


# ---------- статистика ----------
async def adm_stats(uid: int):
    s = db.stats(config.BOT_TZ)
    cats = visible_categories(uid)
    lines = ["Статистика бота"]
    if can_broadcast(uid):
        lines.append(f"Подписчиков: {s['users_total']} (согласие дали: {s['users_consent']})")
    new = sum(s["by_category"].get(k, {}).get("new", 0) for k in cats)
    work = sum(s["by_category"].get(k, {}).get("in_work", 0) for k in cats)
    done = sum(s["by_category"].get(k, {}).get("done", 0) for k in cats)
    over = sum(s["by_category"].get(k, {}).get("overdue", 0) for k in cats)
    lines += [f"Новых: {new}, в работе: {work}, исполнено: {done}", f"Просрочено: {over}", "",
              "Активные заявки по разделам:"]
    for key in cats:
        c = s["by_category"].get(key, {})
        lines.append(f"• {CATEGORIES[key]['title']}: {c.get('new', 0) + c.get('in_work', 0)}")
    if is_admin(uid):
        lines += ["", dates_text()]
    await send(uid, "\n".join(lines), [[("← Назад", "adm:home")]])


# ---------- сообщение всем подписчикам ----------
_drafts: dict[int, list[tuple[str, bytes]]] = {}
_bc_running = False


async def start_broadcast(author_id: int, text: str, files: list[tuple[str, bytes]]) -> int:
    """Запускает рассылку всем подписчикам. Ошибки: ValueError('busy') или ValueError('no_recipients')."""
    global _bc_running
    if _bc_running:
        raise ValueError("busy")
    ids = db.all_user_ids()
    if not ids:
        raise ValueError("no_recipients")
    _bc_running = True
    bc_id = db.create_broadcast(author_id, text, [n for n, _ in files], len(ids))
    asyncio.create_task(_run_broadcast(bc_id, author_id, text, files, ids))
    return bc_id


async def _run_broadcast(bc_id: int, author_id: int, text: str, files, ids: list[int]):
    global _bc_running
    ok = failed = 0
    status = "done"
    try:
        uploads = [await process_input_media(bot, bot, InputMediaBuffer(data, name)) for name, data in files]
        for i, uid in enumerate(ids, 1):
            if await send(uid, text, [[("🏠 Меню", "go:main")]], uploads, fallback=False):
                ok += 1
            else:
                failed += 1
            if i % 10 == 0:
                db.update_broadcast(bc_id, ok, failed, "sending")
            await asyncio.sleep(config.BROADCAST_DELAY)
    except Exception:
        log.exception("Рассылка №%s прервана ошибкой", bc_id)
        status = "failed"
    finally:
        _bc_running = False
        db.update_broadcast(bc_id, ok, failed, status)
    log.info("Рассылка №%s завершена: доставлено %s из %s", bc_id, ok, len(ids))
    if author_id:
        result = (f"доставлено {ok} из {len(ids)}, не доставлено {failed}" if status == "done"
                  else f"остановлена из-за ошибки (доставлено {ok} из {len(ids)})")
        await send(author_id, f"📢 Рассылка №{bc_id}: {result}.", [[("← Панель", "adm:home")]])


async def adm_bc_start(uid: int):
    if not can_broadcast(uid):
        return await deny(uid)
    _drafts.pop(uid, None)
    db.set_session(uid, "admin_broadcast", {})
    await send(
        uid,
        f"📢 Сообщение всем подписчикам бота. Получателей: {db.count_users()}.\n\n"
        "Отправьте текст сообщения. Можно добавить смайлики и приложить файл или фото. "
        "Перед отправкой вы увидите предпросмотр.",
        [[("Отмена", "adm:home")]],
    )


async def adm_bc_draft(uid: int, text: str, atts: list[dict]):
    text = (text or "").strip()
    if not text and not atts:
        await send(uid, "Отправьте текст сообщения или файл.", [[("Отмена", "adm:home")]])
        return
    if len(text) > 3500:
        await send(uid, "Слишком длинный текст (не более 3500 символов). Сократите.", [[("Отмена", "adm:home")]])
        return
    try:
        files = await download_files(atts) if atts else []
    except ValueError as e:
        await send(uid, f"⚠️ {e}", [[("Отмена", "adm:home")]])
        return
    _drafts[uid] = files
    db.set_session(uid, "admin_broadcast_confirm", {"text": text, "names": [n for n, _ in files]})
    preview = "Предпросмотр сообщения:\n\n" + (text or "(без текста)")
    if files:
        preview += "\n\nФайлы: " + ", ".join(n for n, _ in files)
    preview += f"\n\nОтправить {db.count_users()} подписчикам?"
    await send(uid, preview, [[("✅ Отправить всем", "adm:bc_go")], [("✏️ Изменить", "adm:bc"), ("Отмена", "adm:home")]])


async def adm_bc_go(uid: int):
    if not can_broadcast(uid):
        return await deny(uid)
    sess = db.get_session(uid)
    if not sess or sess["state"] != "admin_broadcast_confirm":
        return await adm_bc_start(uid)
    text, names = sess["data"]["text"], sess["data"]["names"]
    files = _drafts.pop(uid, [])
    if names and len(files) != len(names):
        await send(uid, "Файлы потерялись (бот перезапускался). Отправьте сообщение ещё раз.", [[("📢 Заново", "adm:bc")]])
        return
    db.clear_session(uid)
    try:
        bc_id = await start_broadcast(uid, text, files)
    except ValueError as e:
        msg = "Предыдущая рассылка ещё идёт. Подождите её завершения." if str(e) == "busy" else "Пока нет подписчиков."
        await send(uid, msg, [[("← Панель", "adm:home")]])
        return
    await send(uid, f"📢 Рассылка №{bc_id} запущена. Сообщу, когда завершится.", [[("← Панель", "adm:home")]])


# ---------- контакты ----------
def contact_title(u: dict) -> str:
    return (u.get("full_name") or u.get("name") or f"ID {u['user_id']}")[:28]


async def adm_ct_menu(uid: int):
    if not can_edit_contacts(uid):
        return await deny(uid)
    db.set_session(uid, "admin_ct_search", {})
    users, total = db.list_users("", 8)
    rows = [[(f"{contact_title(u)} · {u['user_id']}", f"adm:cu:{u['user_id']}")] for u in users]
    rows.append([("← Назад", "adm:home")])
    await send(uid, f"👥 Контакты бота (всего {total}).\n\nНапишите ID, имя, ФИО или должность для поиска "
                    f"либо выберите из последних:", rows)


async def adm_ct_search(uid: int, text: str):
    users, total = db.list_users(text, 10)
    if not users:
        await send(uid, "Никого не нашёл. Попробуйте другой запрос.", [[("← Назад", "adm:ct")]])
        return
    rows = [[(f"{contact_title(u)} · {u['user_id']}", f"adm:cu:{u['user_id']}")] for u in users]
    rows.append([("← Назад", "adm:ct")])
    await send(uid, f"Найдено: {total}" + (" (показаны первые 10)" if total > 10 else ""), rows)


async def adm_ct_card(uid: int, target: int):
    if not can_edit_contacts(uid):
        return await deny(uid)
    u = db.get_user(target)
    if not u:
        await send(uid, "Контакт не найден.", [[("← Назад", "adm:ct")]])
        return
    db.clear_session(uid)
    text = "\n".join([
        f"Контакт ID {u['user_id']}",
        f"Имя в Max: {u['name'] or '—'}",
        f"ФИО: {u['full_name'] or '—'}",
        f"Должность: {u['position'] or '—'}",
        f"Пометка: {u['note'] or '—'}",
        f"Согласие на обработку данных: {'да' if u['consent'] else 'нет'}",
        f"В боте с {fmt_dt(u['created_at'])}",
    ])
    rows = [[("✏️ ФИО", f"adm:ce:{target}:full_name"), ("✏️ Должность", f"adm:ce:{target}:position")],
            [("✏️ Пометка", f"adm:ce:{target}:note")], [("← К контактам", "adm:ct")]]
    await send(uid, text, rows)


async def adm_ct_edit_start(uid: int, target: int, field: str):
    if not can_edit_contacts(uid):
        return await deny(uid)
    if field not in CONTACT_LABELS or not db.get_user(target):
        return
    db.set_session(uid, "admin_ct_edit", {"uid": target, "field": field})
    await send(uid, f"Введите новое значение поля «{CONTACT_LABELS[field]}» для ID {target}.\n"
                    f"Отправьте «-», чтобы очистить поле.", [[("Отмена", f"adm:cu:{target}")]])


async def adm_ct_edit_save(uid: int, sess: dict, text: str):
    target, field = sess["data"]["uid"], sess["data"]["field"]
    value = "" if text.strip() == "-" else text.strip()
    limit = db.CONTACT_FIELDS[field]
    if len(value) > limit:
        await send(uid, f"Слишком длинно (не более {limit} символов). Сократите.", [[("Отмена", f"adm:cu:{target}")]])
        return
    db.update_contact(target, field, value)
    await adm_ct_card(uid, target)


# ---------- разбор кнопок панели ----------
async def handle_admin(uid: int, parts: list[str]):
    if not is_staff(uid):
        await send(uid, "Нет доступа.", [HOME_ROW])
        return
    action = parts[1] if len(parts) > 1 else "home"
    if action in ("dates", "setdate") and not is_admin(uid):
        await send(uid, "Менять даты выплат может только администратор.", [[("← Панель", "adm:home")]])
        return
    if action == "home":
        await adm_home(uid)
    elif action == "cats":
        await adm_cats(uid)
    elif action == "stats":
        await adm_stats(uid)
    elif action == "list":
        await adm_list(uid, parts[2], parts[3] if len(parts) > 3 else "a")
    elif action == "req":
        db.clear_session(uid)
        await adm_request(uid, int(parts[2]))
    elif action == "st":
        await adm_status(uid, int(parts[2]), parts[3])
    elif action == "reply":
        await adm_reply_start(uid, int(parts[2]))
    elif action == "dates":
        await adm_dates(uid)
    elif action == "setdate":
        await adm_setdate_start(uid, parts[2])
    elif action == "bc":
        await adm_bc_start(uid)
    elif action == "bc_go":
        await adm_bc_go(uid)
    elif action == "ct":
        await adm_ct_menu(uid)
    elif action == "cu":
        await adm_ct_card(uid, int(parts[2]))
    elif action == "ce":
        await adm_ct_edit_start(uid, int(parts[2]), parts[3])


# ======================================================================
# Разбор нажатий кнопок и текста
# ======================================================================
async def handle_payload(uid: int, name: str, payload: str):
    kind, _, rest = payload.partition(":")
    if kind == "adm":
        await handle_admin(uid, payload.split(":"))
        return
    if kind == "consent":
        db.upsert_user(uid, name)
        db.set_consent(uid)
        await show_screen(uid, "main")
        return
    if not await ensure_consent(uid, name):
        return
    if kind == "go" and rest in SCREENS:
        await show_screen(uid, rest)
    elif kind == "info" and rest in INFO:
        info = INFO[rest]
        value = db.get_setting(info["setting"]) or DATE_NOT_SET
        db.clear_session(uid)
        await send(uid, info["text"].format(value=value), back_rows(info["screen"]))
    elif kind == "flow" and rest in FLOWS:
        await start_flow(uid, rest)
    elif kind == "inst" and rest in INSTANT:
        item = INSTANT[rest]
        db.clear_session(uid)
        await make_request(uid, name, item["category"], item["fields"])
        await send(uid, item["done_text"], back_rows(item["screen"]))
    else:
        await show_screen(uid, "main")


async def handle_text(uid: int, name: str, text: str, atts: list[dict] | None = None):
    text = (text or "").strip()
    atts = atts or []

    if text.startswith("/"):
        cmd = text.split()[0].split("@")[0].lower()
        if cmd == "/myid":
            await send(uid, f"Ваш ID в Max: {uid}")
            return
        if cmd == "/admin":
            if is_staff(uid):
                await adm_home(uid)
            else:
                await send(uid, "Нет доступа.")
            return
        if not await ensure_consent(uid, name):
            return
        await show_screen(uid, "main")  # /start, /menu и любые другие команды
        return

    if not await ensure_consent(uid, name):
        return
    sess = db.get_session(uid)
    if not sess:
        await show_screen(uid, "main")
        return
    state = sess["state"]
    if state == "admin_reply" and is_staff(uid):
        await adm_reply_send(uid, sess, text, atts)
    elif state in ("admin_broadcast", "admin_broadcast_confirm") and can_broadcast(uid):
        await adm_bc_draft(uid, text, atts)
    elif not text:
        await send(uid, "Пожалуйста, отправьте ответ текстом.")
    elif state == "flow":
        await flow_answer(uid, name, sess, text)
    elif state == "admin_setdate" and is_admin(uid):
        await adm_setdate_save(uid, sess, text)
    elif state == "admin_ct_search" and can_edit_contacts(uid):
        await adm_ct_search(uid, text)
    elif state == "admin_ct_edit" and can_edit_contacts(uid):
        await adm_ct_edit_save(uid, sess, text)
    else:
        await show_screen(uid, "main")


# ======================================================================
# Обработчики событий Max
# ======================================================================
@dp.bot_started()
async def on_bot_started(event: BotStarted):
    uid = event.user.user_id
    try:
        if await ensure_consent(uid, display_name(event.user)):
            await show_screen(uid, "main")
    except Exception:
        log.exception("Ошибка при запуске бота пользователем %s", uid)


@dp.message_created()
async def on_message(event: MessageCreated):
    sender = event.message.sender
    uid = sender.user_id
    text = event.message.body.text if event.message.body else ""
    try:
        await handle_text(uid, display_name(sender), text, extract_attachments(event.message))
    except Exception:
        log.exception("Ошибка обработки сообщения от %s", uid)
        await send(uid, "Произошла ошибка. Попробуйте ещё раз или вернитесь в главное меню.", [HOME_ROW])


@dp.message_callback()
async def on_callback(event: MessageCallback):
    user = event.callback.user
    uid = user.user_id
    try:
        await handle_payload(uid, display_name(user), event.callback.payload or "")
    except Exception:
        log.exception("Ошибка обработки кнопки от %s", uid)
        await send(uid, "Произошла ошибка. Попробуйте ещё раз или вернитесь в главное меню.", [HOME_ROW])


# ======================================================================
# Действия из веб-дашборда
# ======================================================================
async def dash_reply(req_id: int, text: str, files: list[tuple[str, bytes]]):
    """Ответ работнику из дашборда. None — заявки нет; иначе True/False: доставлено ли."""
    req = db.get_request(req_id)
    if not req:
        return None
    body = f"Ответ по вашей заявке №{req_id}:\n\n{text}" if text else f"Ответ по вашей заявке №{req_id}: вложение"
    delivered = await send(req["user_id"], body, [HOME_ROW], to_media(files))
    db.add_reply(req_id, 0, text, [n for n, _ in files])  # 0 = ответ дан из дашборда
    return delivered


async def dash_status(req_id: int, status: str) -> bool:
    req = db.get_request(req_id)
    if not req or status not in STATUS_TITLES:
        return False
    db.set_status(req_id, status)
    await send(req["user_id"], f"Статус вашей заявки №{req_id} изменён: {STATUS_TITLES[status]}.", [HOME_ROW])
    return True


async def dash_broadcast(text: str, files: list[tuple[str, bytes]]) -> int:
    return await start_broadcast(0, text, files)


def broadcast_running() -> bool:
    return _bc_running


ACTIONS = SimpleNamespace(reply=dash_reply, status=dash_status, broadcast=dash_broadcast,
                          broadcast_running=broadcast_running)


# ======================================================================
# Напоминания о сроках
# ======================================================================
async def reminder_loop():
    while True:
        try:
            now = datetime.now(timezone.utc).strftime(db.FMT)
            for req in db.due_for_reminder(config.REMIND_BEFORE_HOURS):
                overdue = req["due_at"] < now
                head = "⚠️ Срок ответа по заявке истёк" if overdue else "⏰ Приближается срок ответа по заявке"
                for rid in notify_ids(req["category"]):
                    await send(rid, f"{head}\n\n{request_card(req)}",
                               [[("Открыть заявку", f"adm:req:{req['id']}")]])
                db.mark_reminded(req["id"])
        except Exception:
            log.exception("Ошибка в цикле напоминаний")
        await asyncio.sleep(config.REMINDER_CHECK_SECONDS)


async def main():
    global bot
    if not config.BOT_TOKEN:
        raise SystemExit("Не задан токен: добавьте переменную окружения MAX_BOT_TOKEN")
    db.init()
    db.interrupt_broadcasts()
    await dashboard.start(ACTIONS)
    bot = Bot(config.BOT_TOKEN)
    try:
        await bot.delete_webhook()  # если был webhook, polling не получит события
    except Exception:
        log.info("Webhook не удалён (возможно, его не было)")
    asyncio.create_task(reminder_loop())
    log.info("Бот запущен. Админы: %s, ответственные: %s, бухгалтеры: %s",
             sorted(config.ADMIN_IDS), sorted(config.RESPONSIBLE_IDS), sorted(config.ACCOUNTANT_IDS))
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
