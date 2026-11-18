"""Настройки бота. Всё берётся из переменных окружения (Bothost → «Переменные окружения»)."""
import os
from pathlib import Path


def _ids(name: str, default: str = "") -> set[int]:
    raw = os.getenv(name, default)
    return {int(x) for x in raw.replace(";", ",").replace(" ", ",").split(",") if x.strip().isdigit()}


BOT_TOKEN = os.getenv("MAX_BOT_TOKEN") or os.getenv("BOT_TOKEN", "")

# Администраторы: меняют даты выплат, видят и обрабатывают заявки.
ADMIN_IDS = _ids("ADMIN_IDS", "329632295")
# Ответственные: получают уведомления о заявках, видят и обрабатывают их, отвечают работникам.
RESPONSIBLE_IDS = _ids("RESPONSIBLE_IDS", "245949538")
STAFF_IDS = ADMIN_IDS | RESPONSIBLE_IDS

BOT_TZ = os.getenv("BOT_TZ", "Europe/Moscow")
REMIND_BEFORE_HOURS = float(os.getenv("REMIND_BEFORE_HOURS", "4"))
REMINDER_CHECK_SECONDS = int(os.getenv("REMINDER_CHECK_SECONDS", "600"))

# База хранится в /app/data: на Bothost эта папка сохраняется между обновлениями.
DATA_DIR = Path(os.getenv("DATA_DIR", "/app/data"))
try:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
except OSError:  # запуск на своём компьютере
    DATA_DIR = Path(__file__).parent / "data"
    DATA_DIR.mkdir(parents=True, exist_ok=True)
DB_PATH = DATA_DIR / "bot.db"

CONSENT_TEXT = os.getenv(
    "CONSENT_TEXT",
    "Для работы бота мы обрабатываем ваши персональные данные (ФИО, ID в Max, "
    "сведения из заявок) только для рассмотрения ваших обращений. "
    "Нажимая «Согласен(на)», вы даёте согласие на их обработку.",
)
