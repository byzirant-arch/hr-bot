"""Проверка данных, которые вводит работник. Каждая функция возвращает (успех, значение_или_текст_ошибки)."""
import re
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import config

MAX_LEN = 1000


def _clean(text: str) -> str:
    return re.sub(r"\s+", " ", (text or "").strip())


def today():
    return datetime.now(ZoneInfo(config.BOT_TZ)).date()


def parse_date(text: str):
    s = _clean(text).rstrip(".")
    for fmt in ("%d.%m.%Y", "%d.%m.%y", "%d/%m/%Y", "%d-%m-%Y", "%d,%m,%Y"):
        try:
            return datetime.strptime(s, fmt).date()
        except ValueError:
            pass
    if re.fullmatch(r"\d{1,2}[./-]\d{1,2}", s):  # без года: ближайшая такая дата
        try:
            d = datetime.strptime(s.replace("/", ".").replace("-", ".") + f".{today().year}",
                                  "%d.%m.%Y").date()
        except ValueError:
            return None
        return d if d >= today() else d.replace(year=d.year + 1)
    return None


def v_amount(text):
    s = _clean(text).lower()
    for junk in ("руб.", "руб", "р.", "₽"):
        s = s.replace(junk, "")
    s = re.sub(r"[\s_]", "", s)
    if not re.fullmatch(r"\d{1,9}", s):
        return False, "Введите сумму числом, например: 15000"
    n = int(s)
    if n <= 0:
        return False, "Сумма должна быть больше нуля. Введите сумму числом, например: 15000"
    if n > 1_000_000:
        return False, "Сумма слишком большая. Проверьте и введите её ещё раз."
    return True, f"{n:,}".replace(",", " ")


def v_future_date(text):
    d = parse_date(text)
    if d is None:
        return False, "Не удалось распознать дату. Введите её в формате ДД.ММ.ГГГГ, например 25.12.2026."
    if d < today():
        return False, "Эта дата уже прошла. Укажите дату начиная с сегодняшнего дня."
    if d > today() + timedelta(days=365):
        return False, "Слишком далёкая дата. Проверьте и введите её ещё раз."
    return True, d.strftime("%d.%m.%Y")


def v_pay_date(text):
    """Дата выплаты, которую задаёт администратор."""
    d = parse_date(text)
    if d is None:
        return False, "Не удалось распознать дату. Введите её в формате ДД.ММ.ГГГГ, например 25.12.2026."
    if d < today() - timedelta(days=30) or d > today() + timedelta(days=400):
        return False, "Дата выглядит ошибочной. Проверьте и введите её ещё раз."
    return True, d.strftime("%d.%m.%Y")


def v_surname(text):
    s = _clean(text)
    if len(s) < 2 or len(s) > 60 or re.search(r"\d", s) or not re.search(r"[^\W\d_]", s):
        return False, "Введите фамилию буквами, например: Иванов"
    return True, s


def v_fio(text):
    s = _clean(text)
    words = s.split(" ")
    if (len(s) > 100 or len(words) < 2 or re.search(r"\d", s)
            or any(not re.search(r"[^\W\d_]", w) for w in words)):
        return False, "Введите фамилию, имя и отчество полностью, например: Иванов Иван Иванович"
    return True, s


def v_route(text):
    s = _clean(text)
    if len(s) < 5 or len(s) > 200 or not re.search(r"[^\W\d_]", s):
        return False, "Укажите маршрут: откуда и куда, например: Москва — Красноярск"
    return True, s


def v_text(text):
    s = _clean(text)
    if len(s) < 3:
        return False, "Ответ слишком короткий. Опишите подробнее."
    if len(s) > 500:
        return False, "Слишком длинный текст (не более 500 символов). Сократите, пожалуйста."
    return True, s


_EMAIL = re.compile(r"^[\w.+-]+@[\w-]+(\.[\w-]+)+$")


def v_contact(text):
    s = _clean(text)
    if "@" in s:
        if not _EMAIL.match(s):
            return False, "Похоже, адрес электронной почты введён с ошибкой. Пример: name@example.ru"
        return True, s
    if len(s) < 5 or len(s) > 200:
        return False, "Укажите адрес электронной почты или иной способ получения (не короче 5 символов)."
    return True, s


VALIDATORS = {
    "amount": v_amount,
    "future_date": v_future_date,
    "pay_date": v_pay_date,
    "surname": v_surname,
    "fio": v_fio,
    "route": v_route,
    "text": v_text,
    "contact": v_contact,
}


def check(name: str, text: str):
    if len(text or "") > MAX_LEN:
        return False, "Слишком длинное сообщение. Сократите, пожалуйста."
    return VALIDATORS[name](text)
