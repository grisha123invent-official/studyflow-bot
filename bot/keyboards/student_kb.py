"""
Клавиатуры для студентов.
"""
from aiogram.types import (
    ReplyKeyboardMarkup,
    KeyboardButton,
    InlineKeyboardMarkup,
    InlineKeyboardButton,
    ReplyKeyboardRemove,
)
from aiogram.utils.keyboard import InlineKeyboardBuilder


# Доступные типы работ
WORK_TYPES = {
    "Курсовая": "kursovaya",
    "Реферат": "referat",
    "Статья": "statya",
    "Домашка": "domashka",
    "Конспект": "konspekt",
    "Презентация": "prezentaciya",
    "Другое": "drugoe",
}

# Человекочитаемые названия типов
WORK_TYPE_NAMES = {v: k for k, v in WORK_TYPES.items()}


def work_type_kb() -> ReplyKeyboardMarkup:
    """Клавиатура выбора типа работы"""
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="Курсовая"), KeyboardButton(text="Реферат")],
            [KeyboardButton(text="Статья"), KeyboardButton(text="Домашка")],
            [KeyboardButton(text="Конспект"), KeyboardButton(text="Презентация")],
            [KeyboardButton(text="Другое"), KeyboardButton(text="◀️ Назад")],
        ],
        resize_keyboard=True,
        one_time_keyboard=True,
    )


def skip_kb(button_text: str = "Пропустить") -> ReplyKeyboardMarkup:
    """Клавиатура с кнопкой «Пропустить» (для необязательных шагов)"""
    return ReplyKeyboardMarkup(
        keyboard=[[KeyboardButton(text=button_text)]],
        resize_keyboard=True,
        one_time_keyboard=True,
    )


def nav_kb() -> ReplyKeyboardMarkup:
    """Навигационные кнопки: Назад + Новая заявка (для текстовых шагов)"""
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="◀️ Назад"), KeyboardButton(text="🔄 Новая заявка")],
        ],
        resize_keyboard=True,
    )


def skip_nav_kb() -> ReplyKeyboardMarkup:
    """Пропустить + навигация (для необязательных шагов)"""
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="Пропустить")],
            [KeyboardButton(text="◀️ Назад"), KeyboardButton(text="🔄 Новая заявка")],
        ],
        resize_keyboard=True,
    )


def confirm_kb(order_id: int) -> InlineKeyboardMarkup:
    """Инлайн-клавиатура подтверждения заявки"""
    builder = InlineKeyboardBuilder()
    builder.row(
        InlineKeyboardButton(text="✅ Подтвердить", callback_data=f"confirm_order_{order_id}"),
        InlineKeyboardButton(text="✏️ Изменить", callback_data=f"edit_order_{order_id}"),
    )
    builder.row(
        InlineKeyboardButton(text="❌ Отменить", callback_data=f"cancel_order_{order_id}"),
    )
    return builder.as_markup()


def main_menu_kb(role: str = "student") -> ReplyKeyboardMarkup:
    """Основное меню с учетом роли"""
    keyboard = []

    if role == "student":
        keyboard.append([KeyboardButton(text="📋 Новая заявка")])
        keyboard.append([KeyboardButton(text="📂 Мои заказы")])
        keyboard.append([KeyboardButton(text="🔗 Реферальная программа")])
        keyboard.append([KeyboardButton(text="💰 Мой баланс")])
        keyboard.append([KeyboardButton(text="📄 Документы"), KeyboardButton(text="ℹ️ Помощь")])
    else:
        # editor: только свои задания
        keyboard.append([KeyboardButton(text="📋 Мои задания")])

    return ReplyKeyboardMarkup(
        keyboard=keyboard,
        resize_keyboard=True,
    )


def remove_kb() -> ReplyKeyboardRemove:
    """Убирает reply-клавиатуру"""
    return ReplyKeyboardRemove()


def payment_kb(confirmation_url: str) -> InlineKeyboardMarkup:
    """Кнопка для перехода к оплате"""
    builder = InlineKeyboardBuilder()
    builder.row(
        InlineKeyboardButton(text="💳 Оплатить", url=confirmation_url)
    )
    return builder.as_markup()


def price_confirm_kb(order_id: int) -> InlineKeyboardMarkup:
    """Инлайн-кнопки для студента: подтвердить / торг / отменить"""
    builder = InlineKeyboardBuilder()
    builder.row(
        InlineKeyboardButton(text="✅ Согласен", callback_data=f"student_price_ok_{order_id}"),
    )
    builder.row(
        InlineKeyboardButton(text="💬 Предложить свою цену", callback_data=f"student_price_offer_{order_id}"),
    )
    builder.row(
        InlineKeyboardButton(text="❌ Отменить заявку", callback_data=f"student_price_cancel_{order_id}"),
    )
    return builder.as_markup()


def admin_price_negotiation_kb(order_id: int) -> InlineKeyboardMarkup:
    """Инлайн-кнопки для админа на контрпредложение студента."""
    builder = InlineKeyboardBuilder()
    builder.row(
        InlineKeyboardButton(text="✅ Согласиться", callback_data=f"admin_negot_accept_{order_id}"),
    )
    builder.row(
        InlineKeyboardButton(text="💬 Предложить свою цену", callback_data=f"admin_negot_counter_{order_id}"),
    )
    builder.row(
        InlineKeyboardButton(text="❌ Отклонить заявку", callback_data=f"admin_negot_reject_{order_id}"),
    )
    return builder.as_markup()


def konspekt_source_kb() -> ReplyKeyboardMarkup:
    """Клавиатура выбора типа источника для конспекта"""
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="🎓 Лекция"), KeyboardButton(text="💻 Вебинар")],
            [KeyboardButton(text="📚 Семинар"), KeyboardButton(text="🎙 Подкаст")],
            [KeyboardButton(text="🎬 Мастер-класс"), KeyboardButton(text="📌 Другое")],
            [KeyboardButton(text="◀️ Назад"), KeyboardButton(text="🔄 Новая заявка")],
        ],
        resize_keyboard=True,
    )


KONSPEKT_SOURCE_TYPES = {
    "🎓 Лекция", "💻 Вебинар", "📚 Семинар",
    "🎙 Подкаст", "🎬 Мастер-класс", "📌 Другое",
}


def konspekt_materials_kb() -> ReplyKeyboardMarkup:
    """Клавиатура для шага материалов конспекта"""
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="Пропустить")],
            [KeyboardButton(text="◀️ Назад"), KeyboardButton(text="🔄 Новая заявка")],
        ],
        resize_keyboard=True,
    )


def withdrawal_kb() -> InlineKeyboardMarkup:
    """Кнопка для запроса вывода средств"""
    builder = InlineKeyboardBuilder()
    builder.row(
        InlineKeyboardButton(text="💸 Вывести средства", callback_data="request_withdrawal")
    )
    return builder.as_markup()


# ─── URL-документы (общие константы из config) ───────────────────────────────
from config import settings

DOC_URLS = {
    "oferta": f"{settings.PUBLIC_URL}/oferta",
    "privacy": f"{settings.PUBLIC_URL}/privacy",
    "agreement": f"{settings.PUBLIC_URL}/agreement",
    "refund": f"{settings.PUBLIC_URL}/refund",
}


def docs_kb() -> InlineKeyboardMarkup:
    """Кнопки со ссылками на все 4 документа."""
    builder = InlineKeyboardBuilder()
    builder.row(
        InlineKeyboardButton(text="📄 Публичная оферта", url=DOC_URLS["oferta"])
    )
    builder.row(
        InlineKeyboardButton(text="🔒 Политика конфиденциальности", url=DOC_URLS["privacy"])
    )
    builder.row(
        InlineKeyboardButton(text="📜 Пользовательское соглашение", url=DOC_URLS["agreement"])
    )
    builder.row(
        InlineKeyboardButton(text="💸 Правила возврата средств", url=DOC_URLS["refund"])
    )
    return builder.as_markup()


def consent_kb() -> InlineKeyboardMarkup:
    """Кнопки для явного согласия с офертой + ссылки на документы."""
    builder = InlineKeyboardBuilder()
    builder.row(
        InlineKeyboardButton(text="📄 Оферта", url=DOC_URLS["oferta"]),
        InlineKeyboardButton(text="🔒 Политика", url=DOC_URLS["privacy"]),
    )
    builder.row(
        InlineKeyboardButton(text="📜 Соглашение", url=DOC_URLS["agreement"]),
        InlineKeyboardButton(text="💸 Возврат", url=DOC_URLS["refund"]),
    )
    builder.row(
        InlineKeyboardButton(text="✅ Согласен, продолжить", callback_data="consent_accept")
    )
    return builder.as_markup()


def preview_actions_kb(order_id: int, payment_url: str | None = None) -> InlineKeyboardMarkup:
    """Кнопки для PREVIEW_SENT/AWAITING_PAYMENT заказа: посмотреть превью + оплатить."""
    builder = InlineKeyboardBuilder()
    builder.row(InlineKeyboardButton(
        text="👀 Посмотреть превью",
        callback_data=f"view_preview_{order_id}",
    ))
    if payment_url:
        builder.row(InlineKeyboardButton(text="💳 Оплатить", url=payment_url))
    else:
        builder.row(InlineKeyboardButton(
            text="💳 Получить ссылку на оплату",
            callback_data=f"get_payment_{order_id}",
        ))
    return builder.as_markup()


def preview_payment_kb(
    order_id: int, payment_url: str | None = None, show_rework: bool = True
) -> InlineKeyboardMarkup:
    """Кнопки под превью+оплатой.

    show_rework=True (по умолчанию): [✏️ На доработку] [💳 Оплатить] в одной строке.
    show_rework=False: только [💳 Оплатить] (когда бесплатные правки исчерпаны).

    Если ``payment_url`` задан — «Оплатить» ведёт прямо на ссылку Robokassa.
    Если None (у студента ещё нет email для чека) — «Оплатить» это callback
    ``get_payment_{id}``, который сперва запросит email (54-ФЗ), затем создаст
    ссылку. Так чек гарантированно уходит студенту, а не на fallback-адрес.
    """
    if payment_url:
        pay_btn = InlineKeyboardButton(text="💳 Оплатить", url=payment_url)
    else:
        pay_btn = InlineKeyboardButton(text="💳 Оплатить", callback_data=f"get_payment_{order_id}")

    builder = InlineKeyboardBuilder()
    if show_rework:
        builder.row(
            InlineKeyboardButton(
                text="✏️ На доработку",
                callback_data=f"preview_rework_{order_id}",
            ),
            pay_btn,
        )
    else:
        builder.row(pay_btn)
    return builder.as_markup()


def cancel_order_kb(order_id: int) -> InlineKeyboardMarkup:
    """Кнопка «Отменить заявку» для PENDING-заказа."""
    builder = InlineKeyboardBuilder()
    builder.row(
        InlineKeyboardButton(
            text="❌ Отменить заявку",
            callback_data=f"stcancel_{order_id}",
        )
    )
    return builder.as_markup()


def cancel_order_confirm_kb(order_id: int) -> InlineKeyboardMarkup:
    """Подтверждение отмены заявки."""
    builder = InlineKeyboardBuilder()
    builder.row(
        InlineKeyboardButton(text="✅ Да, отменить", callback_data=f"stcancel_yes_{order_id}"),
        InlineKeyboardButton(text="◀️ Не отменять", callback_data="stcancel_no"),
    )
    return builder.as_markup()


def refund_order_select_kb(orders) -> InlineKeyboardMarkup:
    """Выбор заказа для возврата (из списка оплаченных)."""
    builder = InlineKeyboardBuilder()
    for o in orders:
        topic = (o.topic or "Без темы")[:25]
        price = f"{o.price:.0f}₽" if o.price else "—"
        builder.row(
            InlineKeyboardButton(
                text=f"#{o.id} · {topic} · {price}",
                callback_data=f"refund_pick_{o.id}",
            )
        )
    builder.row(
        InlineKeyboardButton(text="◀️ Отмена", callback_data="refund_cancel")
    )
    return builder.as_markup()


def revision_request_kb(order_id: int, used: int, max_revisions: int = 2) -> InlineKeyboardMarkup:
    """Кнопка «Запросить правку» для DONE-заказа, с показом счётчика использованных правок."""
    builder = InlineKeyboardBuilder()
    if used < max_revisions:
        remaining = max_revisions - used
        builder.row(
            InlineKeyboardButton(
                text=f"✏️ Запросить правку ({used}/{max_revisions})",
                callback_data=f"revision_{order_id}",
            )
        )
    return builder.as_markup()
