"""
Клавиатуры для администратора и редактора.
"""
from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton, ReplyKeyboardMarkup, KeyboardButton
from aiogram.utils.keyboard import InlineKeyboardBuilder


def admin_order_kb(order_id: int) -> InlineKeyboardMarkup:
    """
    Инлайн-клавиатура для карточки заявки у администратора.
    Кнопки: принять, изменить цену, отклонить, скопировать промпт.
    """
    builder = InlineKeyboardBuilder()
    builder.row(
        InlineKeyboardButton(text="✅ Принять", callback_data=f"accept_{order_id}"),
        InlineKeyboardButton(text="❌ Отклонить", callback_data=f"reject_{order_id}"),
    )
    builder.row(
        InlineKeyboardButton(text="✏️ Изменить цену", callback_data=f"change_price_{order_id}"),
    )
    builder.row(
        InlineKeyboardButton(text="📋 Скопировать промпт", callback_data=f"copy_prompt_{order_id}"),
    )
    return builder.as_markup()


# Типы работ, для которых есть PDF-шаблон оформления
_WORK_TYPES_WITH_TEMPLATE = {"kursovaya", "referat", "statya"}


def admin_after_accept_kb(order_id: int, work_type: str = "") -> InlineKeyboardMarkup:
    """
    Клавиатура после принятия заказа.
    Кнопки: промпт, (если есть шаблон) PDF-шаблон, отправить редактору.
    """
    builder = InlineKeyboardBuilder()
    if work_type in _WORK_TYPES_WITH_TEMPLATE:
        builder.row(
            InlineKeyboardButton(text="📋 Промпт", callback_data=f"copy_prompt_{order_id}"),
            InlineKeyboardButton(text="📎 Шаблон", callback_data=f"send_template_{order_id}"),
        )
    else:
        builder.row(
            InlineKeyboardButton(text="📋 Промпт", callback_data=f"copy_prompt_{order_id}"),
        )
    builder.row(
        InlineKeyboardButton(text="📤 Отправить редактору", callback_data=f"send_editor_{order_id}"),
    )
    return builder.as_markup()


def admin_collect_files_kb() -> ReplyKeyboardMarkup:
    """Клавиатура при сборе файлов для редактора"""
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="✅ Отправить редактору")],
            [KeyboardButton(text="❌ Отмена")],
        ],
        resize_keyboard=True,
    )


def editor_done_kb(order_id: int) -> InlineKeyboardMarkup:
    """Старое имя — оставлено для совместимости. Кнопка «Начать работу»."""
    return editor_task_kb(order_id)


def editor_task_kb(order_id: int) -> InlineKeyboardMarkup:
    """
    Инлайн-клавиатура для редактора — кнопка «🚀 Начать работу».
    После клика редактор переходит в режим сбора финальных файлов.
    """
    builder = InlineKeyboardBuilder()
    builder.row(
        InlineKeyboardButton(text="🚀 Начать работу", callback_data=f"editor_start_{order_id}"),
    )
    return builder.as_markup()


def editor_preview_kb(order_id: int, variant: int) -> InlineKeyboardMarkup:
    """Кнопки редактора после получения превью"""
    builder = InlineKeyboardBuilder()
    builder.row(
        InlineKeyboardButton(text="✅ Отправить студенту", callback_data=f"preview_approve_{order_id}_{variant}"),
        InlineKeyboardButton(text="🔄 Пересоздать", callback_data=f"preview_regen_{order_id}_{variant}"),
    )
    return builder.as_markup()


def editor_collect_kb() -> ReplyKeyboardMarkup:
    """Reply-клавиатура при сборе финальных файлов от редактора"""
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="✅ Подтвердить — отправить студенту")],
            [KeyboardButton(text="❌ Отмена")],
        ],
        resize_keyboard=True,
    )


_STATUS_EMOJI = {
    "FILLING":          "✏️",
    "PENDING":          "🟡",
    "IN_WORK":          "🟠",
    "EDITING":          "🔴",
    "PREVIEW_SENT":     "🔵",
    "AWAITING_PAYMENT": "💳",
    "DONE":             "✅",
    "CANCELLED":        "❌",
    "REFUNDED":         "💸",
}

_STATUS_LABEL = {
    "FILLING":          "Заполняется",
    "PENDING":          "Ожидает",
    "IN_WORK":          "В работе",
    "EDITING":          "У редактора",
    "PREVIEW_SENT":     "Превью",
    "AWAITING_PAYMENT": "Оплата",
    "DONE":             "Готов",
    "CANCELLED":        "Отменён",
    "REFUNDED":         "Возврат",
}

_WORK_SHORT = {
    "kursovaya": "Курсовая",
    "referat":   "Реферат",
    "statya":    "Статья",
    "domashka":  "Домашка",
    "konspekt":     "Конспект",
    "prezentaciya": "Презентация",
    "drugoe":       "Другое",
}


def admin_orders_kb(orders: list) -> InlineKeyboardMarkup:
    """
    Список заказов в виде инлайн-кнопок (для команды /admin_orders).
    Отменённые заказы не показываются.
    """
    builder = InlineKeyboardBuilder()
    for order in orders:
        status_raw = order.status if isinstance(order.status, str) else order.status.value
        if status_raw in ("CANCELLED", "REFUNDED"):
            continue
        emoji = _STATUS_EMOJI.get(status_raw, "⚪")
        status_text = _STATUS_LABEL.get(status_raw, status_raw)
        work_name = _WORK_SHORT.get(order.work_type or "", order.work_type or "—")
        topic = (order.topic or "")[:15]
        # Лимит кнопки Telegram — 64 символа. Формат: эмодзи #id · тип · статус · тема
        label = f"{emoji} #{order.id} · {work_name} · {status_text} · {topic}"
        builder.row(
            InlineKeyboardButton(text=label[:64], callback_data=f"view_order_{order.id}")
        )
    return builder.as_markup()


def withdrawal_action_kb(wd_id: int) -> InlineKeyboardMarkup:
    """Кнопки одобрения/отклонения заявки на вывод"""
    builder = InlineKeyboardBuilder()
    builder.row(
        InlineKeyboardButton(text="✅ Одобрить", callback_data=f"approve_wd_{wd_id}"),
        InlineKeyboardButton(text="❌ Отклонить", callback_data=f"reject_wd_{wd_id}"),
    )
    return builder.as_markup()


def withdrawal_page_kb(offset: int, total: int, page_size: int = 10) -> InlineKeyboardMarkup:
    """Кнопки пагинации для списка заявок на вывод"""
    builder = InlineKeyboardBuilder()
    buttons = []
    if offset > 0:
        buttons.append(InlineKeyboardButton(text="← Назад", callback_data=f"wd_page_{offset - page_size}"))
    if offset + page_size < total:
        buttons.append(InlineKeyboardButton(text="Ещё →", callback_data=f"wd_page_{offset + page_size}"))
    if buttons:
        builder.row(*buttons)
    return builder.as_markup()


def editor_main_kb() -> ReplyKeyboardMarkup:
    """Главная reply-клавиатура редактора"""
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="📋 Мои задания")],
        ],
        resize_keyboard=True,
    )


def admin_main_kb() -> ReplyKeyboardMarkup:
    """Главная reply-клавиатура администратора"""
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="📋 Заявки"), KeyboardButton(text="💸 Выводы")],
            [KeyboardButton(text="📊 Аналитика"), KeyboardButton(text="📂 Все заказы")],
            [KeyboardButton(text="🔗 Рефералы"), KeyboardButton(text="📨 Возвраты")],
        ],
        resize_keyboard=True,
    )


def refund_action_kb(refund_id: int) -> InlineKeyboardMarkup:
    """Кнопки одобрения/отклонения заявки на возврат."""
    builder = InlineKeyboardBuilder()
    builder.row(
        InlineKeyboardButton(text="✅ Одобрить и вернуть", callback_data=f"approve_refund_{refund_id}"),
        InlineKeyboardButton(text="❌ Отклонить", callback_data=f"reject_refund_{refund_id}"),
    )
    return builder.as_markup()
