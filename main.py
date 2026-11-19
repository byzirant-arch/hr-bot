"""Информационный бот для работников в мессенджере MAX (по ТЗ)."""
import asyncio
import logging
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

from maxapi import Bot, Dispatcher
from maxapi.types import BotStarted, CallbackButton, MessageCallback, MessageCreated
from maxapi.utils.inline_keyboard import InlineKeyboardBuilder

import config
import dashboard
import db
import validators
from menu_config import (CATEGORIES, FLOWS, INFO, INSTANT, SCREENS, SETTINGS,
                         STATUS_TITLES, DATE_NOT_SET)

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("hrbot")

dp = Dispatcher()
bot: Bot | None = None
TZ = ZoneInfo(config.BOT_TZ)

HOME_BTN = ("🏠 Главное меню", "go:main")


# ======================================================================
# Вспомогательные функции
# ======================================================================
def is_staff(uid: int) -> bool:
    return uid in config.STAFF_IDS


def is_admin(uid: int) -> bool:
    return uid in config.ADMIN_IDS


def display_name(user) -> str:
    parts = [getattr(user, "first_name", None), getattr(user, "last_name", None)]
    name = " ".join(p for p in parts if p).strip()
    return name or getattr(user, "username", None) or f"ID {user.user_id}"


def keyboard(rows):
    builder = InlineKeyboardBuilder()
    for row in rows:
        builder.row(*[CallbackButton(text=t, payload=p) for t, p in row])
    return [builder.as_markup()]


async def send(uid: int, text: str, rows=None) -> bool:
    """Отправить сообщение пользователю по его ID. Тексты заявок в лог не пишем."""
    try:
        result = await bot.send_message(
            user_id=uid, text=text, attachments=keyboard(rows) if rows else None
        )
        if result.__class__.__name__ == "Error":
            log.warning("Max не принял сообщение для %s: %s", uid, getattr(result, "message", result))
            return False
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
        f"Статус: {STATUS_TITLES.get(req['status'], req['status'])}",
    ]
    if req.get("due_at"):
        lines.append(f"Срок ответа: до {fmt_dt(req['due_at'])}")
    lines.append("Содержание:")
    for label, value in req["fields"]:
        lines.append(f"• {label}: {value}")
    if replies:
        lines.append("Ответы работнику:")
        for r in replies[-3:]:
            lines.append(f"• {fmt_dt(r['created_at'])}: {r['text']}")
    return "\n".join(lines)


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
    for rid in config.RESPONSIBLE_IDS:
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


HOME_ROW = [HOME_BTN]


# ======================================================================
# Панель сотрудника (администратор / ответственный)
# ======================================================================
def admin_home_rows(uid: int):
    rows = [[("📥 Заявки", "adm:cats")], [("📊 Статистика", "adm:stats")]]
    if is_admin(uid):
        rows.append([("📅 Даты выплат", "adm:dates")])
    rows.append([HOME_BTN])
    return rows


async def adm_home(uid: int):
    db.clear_session(uid)
    await send(uid, "Панель сотрудника. Выберите действие:", admin_home_rows(uid))


async def adm_cats(uid: int):
    counts = db.count_active_by_category()
    rows = []
    for key, cat in CATEGORIES.items():
        rows.append([(f"{cat['title']} ({counts.get(key, 0)})", f"adm:list:{key}:a")])
    rows.append([("← Назад", "adm:home")])
    await send(uid, "Заявки по категориям (в скобках — активные: новые и в работе):", rows)


async def adm_list(uid: int, cat: str, mode: str):
    if cat not in CATEGORIES:
        return
    closed = mode == "c"
    items = db.list_requests(cat, closed)
    title = CATEGORIES[cat]["title"]
    rows = []
    for r in items:
        short = r["user_name"][:25]
        rows.append([(f"№{r['id']} · {short} · {STATUS_TITLES[r['status']]}", f"adm:req:{r['id']}")])
    rows.append([("📁 Архив" if not closed else "📂 Активные", f"adm:list:{cat}:{'c' if not closed else 'a'}")])
    rows.append([("← К категориям", "adm:cats")])
    head = f"{title} — {'архив (исполненные и отклонённые)' if closed else 'активные заявки'}"
    if not items:
        head += "\n\nЗаявок нет."
    await send(uid, head, rows)


async def adm_request(uid: int, req_id: int):
    req = db.get_request(req_id)
    if not req:
        await send(uid, "Заявка не найдена.", [[("← К категориям", "adm:cats")]])
        return
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
    db.set_status(req_id, status)
    await send(
        req["user_id"],
        f"Статус вашей заявки №{req_id} изменён: {STATUS_TITLES[status]}.",
        [HOME_ROW],
    )
    await adm_request(uid, req_id)


async def adm_reply_start(uid: int, req_id: int):
    if not db.get_request(req_id):
        return
    db.set_session(uid, "admin_reply", {"request_id": req_id})
    await send(
        uid,
        f"Введите текст ответа работнику по заявке №{req_id}. Он будет отправлен через бота.",
        [[("Отмена", f"adm:req:{req_id}")]],
    )


async def adm_reply_send(uid: int, sess: dict, text: str):
    req_id = sess["data"]["request_id"]
    req = db.get_request(req_id)
    text = (text or "").strip()
    if not req or not text:
        await send(uid, "Введите текст ответа.", [[("Отмена", f"adm:req:{req_id}")]])
        return
    if len(text) > 1500:
        await send(uid, "Слишком длинный ответ (не более 1500 символов). Сократите.",
                   [[("Отмена", f"adm:req:{req_id}")]])
        return
    delivered = await send(req["user_id"], f"Ответ по вашей заявке №{req_id}:\n\n{text}", [HOME_ROW])
    db.add_reply(req_id, uid, text)
    db.clear_session(uid)
    if not delivered:
        await send(uid, "⚠️ Ответ сохранён, но доставить его работнику не удалось "
                        "(возможно, он ещё не запускал бота).")
    await adm_request(uid, req_id)


async def adm_stats(uid: int):
    s = db.stats(config.BOT_TZ)
    st = s["by_status"]
    lines = [
        "Статистика бота",
        f"Работников в боте: {s['users_total']} (согласие дали: {s['users_consent']})",
        f"Заявок всего: {s['requests_total']}",
        f"Новых: {st['new']}, в работе: {st['in_work']}, исполнено: {st['done']}, отклонено: {st['rejected']}",
        f"Просрочено: {s['overdue']}",
        "",
        "Активные заявки по разделам:",
    ]
    for key, cat in CATEGORIES.items():
        c = s["by_category"].get(key, {})
        lines.append(f"• {cat['title']}: {c.get('new', 0) + c.get('in_work', 0)}")
    lines += ["", dates_text()]
    await send(uid, "\n".join(lines), [[("← Назад", "adm:home")]])


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


async def handle_text(uid: int, name: str, text: str):
    text = (text or "").strip()

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
    if not text:
        await send(uid, "Пожалуйста, отправьте ответ текстом.")
        return
    state = sess["state"]
    if state == "flow":
        await flow_answer(uid, name, sess, text)
    elif state == "admin_reply" and is_staff(uid):
        await adm_reply_send(uid, sess, text)
    elif state == "admin_setdate" and is_admin(uid):
        await adm_setdate_save(uid, sess, text)
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
        await handle_text(uid, display_name(sender), text)
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
# Действия из веб-дашборда (ответ и смена статуса)
# ======================================================================
async def dash_reply(req_id: int, text: str):
    """Ответ работнику из дашборда. None — заявки нет; иначе True/False: доставлено ли."""
    req = db.get_request(req_id)
    if not req:
        return None
    delivered = await send(req["user_id"], f"Ответ по вашей заявке №{req_id}:\n\n{text}", [HOME_ROW])
    db.add_reply(req_id, 0, text)  # 0 = ответ дан из дашборда
    return delivered


async def dash_status(req_id: int, status: str) -> bool:
    req = db.get_request(req_id)
    if not req or status not in STATUS_TITLES:
        return False
    db.set_status(req_id, status)
    await send(req["user_id"], f"Статус вашей заявки №{req_id} изменён: {STATUS_TITLES[status]}.", [HOME_ROW])
    return True


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
                for rid in config.RESPONSIBLE_IDS:
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
    await dashboard.start(dash_reply, dash_status)
    bot = Bot(config.BOT_TOKEN)
    try:
        await bot.delete_webhook()  # если был webhook, polling не получит события
    except Exception:
        log.info("Webhook не удалён (возможно, его не было)")
    asyncio.create_task(reminder_loop())
    log.info("Бот запущен. Админы: %s, ответственные: %s",
             sorted(config.ADMIN_IDS), sorted(config.RESPONSIBLE_IDS))
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
