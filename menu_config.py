"""
Меню, сценарии и тексты бота.

ЧТОБЫ ДОБАВИТЬ НОВЫЙ РАЗДЕЛ / ВОПРОС, достаточно изменить этот файл:
  1. CATEGORIES  — тип заявки (название и срок ответа в часах, None = без срока).
  2. FLOWS       — пошаговая анкета (вопросы по порядку + проверка ответа).
     INSTANT     — обращение одной кнопкой (без вопросов).
     INFO        — кнопка, которая показывает сохранённую дату/текст.
  3. SCREENS     — экраны с кнопками. Кнопка ведёт на:
        go:<экран>      другой экран
        flow:<анкета>   запуск анкеты
        inst:<обращение> мгновенное обращение
        info:<ключ>     информационный ответ
Кнопки «Назад» и «Главное меню» добавляются автоматически.

Типы проверки ответа (validator): amount, future_date, surname, fio, route, text, contact.
"""

CATEGORIES = {
    "advance":        {"title": "Заявка на аванс",                 "sla_hours": None},
    "per_diem_issue": {"title": "Обращение по невыплаченным суточным", "sla_hours": 24},
    "ticket_buy":     {"title": "Заявка на приобретение билета",   "sla_hours": None},
    "ticket_when":    {"title": "Запрос о сроках приобретения билета", "sla_hours": 24},
    "certificate":    {"title": "Запрос справки",                  "sla_hours": 72},
    "documents":      {"title": "Запрос документов",               "sla_hours": 24},
}

# Настройки, которые администратор меняет в панели (ключ → название)
SETTINGS = {
    "salary_date":   "Дата выплаты заработной платы",
    "per_diem_date": "Дата выплаты суточных",
}

DATE_NOT_SET = "пока не назначена (уточните позже)"

STATUS_TITLES = {
    "new": "Новая",
    "in_work": "В работе",
    "done": "Исполнена",
    "rejected": "Отклонена",
}

SCREENS = {
    "main": {
        "text": "Здравствуйте! Выберите интересующий вас раздел:",
        "parent": None,
        "buttons": [
            ("Заработная плата", "go:salary"),
            ("Суточные", "go:per_diem"),
            ("Билеты", "go:tickets"),
            ("Справки и запрос документов", "go:docs"),
        ],
    },
    "salary": {
        "text": "Раздел «Заработная плата». Выберите действие:",
        "parent": "main",
        "buttons": [
            ("Когда будет заработная плата?", "info:salary_date"),
            ("Хочу аванс", "flow:advance"),
        ],
    },
    "per_diem": {
        "text": "Раздел «Суточные». Выберите действие:",
        "parent": "main",
        "buttons": [
            ("Когда будет выплата суточных?", "info:per_diem_date"),
            ("Мне не пришли суточные", "inst:per_diem_issue"),
        ],
    },
    "tickets": {
        "text": "Раздел «Билеты». Выберите действие:",
        "parent": "main",
        "buttons": [
            ("Мне нужно приобрести билет", "flow:ticket_buy"),
            ("Когда мне купят билет?", "inst:ticket_when"),
        ],
    },
    "docs": {
        "text": "Раздел «Справки и запрос документов». Выберите действие:",
        "parent": "main",
        "buttons": [
            ("Мне нужна справка", "flow:certificate"),
            ("Мне нужны документы", "flow:documents"),
        ],
    },
}

# Информационные ответы: подставляют значение, заданное администратором
INFO = {
    "salary_date": {
        "text": "Планируемая дата выплаты заработной платы — {value}",
        "setting": "salary_date",
        "screen": "salary",
    },
    "per_diem_date": {
        "text": "Планируемая дата выплаты суточных — {value}",
        "setting": "per_diem_date",
        "screen": "per_diem",
    },
}

ACCEPTED_TODAY = "Ваше обращение принято. Мы изучим вопрос и ответим вам в течение дня."

# Обращения одной кнопкой
INSTANT = {
    "per_diem_issue": {
        "category": "per_diem_issue",
        "screen": "per_diem",
        "fields": [["Суть обращения", "Работник сообщает, что суточные не пришли"]],
        "done_text": ACCEPTED_TODAY,
    },
    "ticket_when": {
        "category": "ticket_when",
        "screen": "tickets",
        "fields": [["Суть обращения", "Работник спрашивает, когда ему купят билет"]],
        "done_text": ACCEPTED_TODAY,
    },
}

CONTACT_HINT = "Укажите адрес электронной почты или иной способ получения."

# Пошаговые анкеты
FLOWS = {
    "advance": {
        "category": "advance",
        "screen": "salary",
        "steps": [
            {"key": "amount", "label": "Сумма аванса, руб.", "validator": "amount",
             "prompt": "Укажите сумму необходимого аванса в рублях"},
        ],
        "done_text": "Ваша заявка на аванс в размере {amount} руб. принята и передана на рассмотрение.",
    },
    "ticket_buy": {
        "category": "ticket_buy",
        "screen": "tickets",
        "steps": [
            {"key": "surname", "label": "Фамилия", "validator": "surname",
             "prompt": "Укажите вашу фамилию"},
            {"key": "date", "label": "Дата поездки", "validator": "future_date",
             "prompt": "Укажите дату поездки в формате ДД.ММ.ГГГГ (например, 25.12.2026)"},
            {"key": "route", "label": "Маршрут", "validator": "route",
             "prompt": "Укажите маршрут: откуда и куда необходимо приобрести билет "
                       "(например: Москва — Красноярск)"},
        ],
        "done_text": "Ваша заявка на приобретение билета принята и передана ответственному сотруднику.",
    },
    "certificate": {
        "category": "certificate",
        "screen": "docs",
        "steps": [
            {"key": "fio", "label": "ФИО", "validator": "fio",
             "prompt": "Укажите ваши фамилию, имя и отчество"},
            {"key": "what", "label": "Какая справка нужна", "validator": "text",
             "prompt": "Какую справку необходимо подготовить?"},
            {"key": "send_to", "label": "Куда направить", "validator": "contact",
             "prompt": "Куда направить готовую справку? " + CONTACT_HINT},
        ],
        "done_text": "Ваш запрос принят. Справка будет подготовлена в течение 3 дней "
                     "и направлена по указанным реквизитам.",
    },
    "documents": {
        "category": "documents",
        "screen": "docs",
        "steps": [
            {"key": "fio", "label": "ФИО", "validator": "fio",
             "prompt": "Укажите ваши фамилию, имя и отчество"},
            {"key": "what", "label": "Перечень документов", "validator": "text",
             "prompt": "Перечислите необходимые документы"},
            {"key": "send_to", "label": "Куда направить", "validator": "contact",
             "prompt": "Куда направить документы? " + CONTACT_HINT},
        ],
        "done_text": "Ваш запрос принят. Документы будут направлены в течение 1 дня.",
    },
}
