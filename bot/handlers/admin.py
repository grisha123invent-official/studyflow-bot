"""
Хендлеры для администратора:
- Принять / отклонить заявку
- Изменить цену
- Скопировать промпт
- Загрузить черновик .docx → отправить редактору
- /admin_orders — список всех заявок
"""
import os
import asyncio
import logging
from aiogram import Router, F, Bot
from aiogram.types import Message, CallbackQuery, Document, FSInputFile
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext

from config import settings
from db.models import OrderStatus, FileType, UserRole, WithdrawalStatus, RefundStatus
from db.session import AsyncSessionLocal
from db.crud import (
    get_order, update_order_status, update_order_price,
    save_file, get_all_orders, get_file, get_user_by_tg_id,
    get_pending_withdrawals, count_pending_withdrawals,
    update_withdrawal_status, get_admin_analytics,
)
from bot.states.order import AdminPriceStates, AdminSendToEditorStates, AdminRefundStates, AdminNegotiationStates
from bot.keyboards.admin_kb import admin_order_kb, admin_after_accept_kb, editor_done_kb, admin_collect_files_kb
from bot.keyboards.student_kb import WORK_TYPE_NAMES, price_confirm_kb
from bot.filters import RoleFilter

# PDF-шаблоны оформления по типу работы
_PDF_TEMPLATE_MAP = {
    "kursovaya": "оформление курсовой.pdf",
    "referat":   "оформление реферата.pdf",
    "statya":    "оформление статьи.pdf",
}

logger = logging.getLogger(__name__)
router = Router()

# Человекочитаемые названия статусов заказа
# ВАЖНО: ключи должны соответствовать OrderStatus.<NAME>.value (UPPER_CASE)
STATUS_NAMES = {
    "FILLING":          "✏️ Заполняется",
    "PENDING":          "🟡 Ожидает рассмотрения",
    "IN_WORK":          "🟠 В работе",
    "EDITING":          "🔴 У редактора",
    "PREVIEW_SENT":     "🔵 Превью отправлено",
    "AWAITING_PAYMENT": "💳 Ожидает оплаты",
    "DONE":             "✅ Выполнен",
    "CANCELLED":        "❌ Отменён",
    "REFUNDED":         "💸 Деньги возвращены",
}


def _is_admin(user) -> bool:
    return user.role == UserRole.ADMIN.value


# ─── Принять заявку ───────────────────────────────────────────────────────────

@router.callback_query(F.data.startswith("accept_"))
async def cb_accept_order(callback: CallbackQuery, user, bot: Bot) -> None:
    if not _is_admin(user):
        await callback.answer("Нет доступа.", show_alert=True)
        return

    order_id = int(callback.data.split("_")[-1])

    async with AsyncSessionLocal() as session:
        order = await get_order(session, order_id)
        if order is None:
            await callback.answer("Заказ не найден.", show_alert=True)
            return

        # НЕ переводим в работу здесь: заявка идёт студенту на согласование цены.
        # В работу (IN_WORK) переведём только после подтверждения студентом
        # (см. cb_student_price_ok в student.py).
        price = order.price or 0.0

        # Уведомляем студента с просьбой подтвердить цену
        try:
            await bot.send_message(
                chat_id=order.user.tg_id,
                text=(
                    f"🔔 <b>Ваша заявка #{order_id} рассмотрена!</b>\n\n"
                    f"Цена: <b>{price:.0f} руб.</b>\n\n"
                    f"Подтвердите цену — и работа начнётся.\n"
                    f"Если не согласны — можете отменить заявку."
                ),
                reply_markup=price_confirm_kb(order_id),
                parse_mode="HTML",
            )
        except Exception as e:
            logger.error(f"Не удалось уведомить студента: {e}")

    await callback.message.edit_text(
        callback.message.text
        + "\n\n⏳ <b>Цена отправлена студенту на подтверждение</b>\n"
        "Как только студент согласится — придёт уведомление с кнопкой «Отправить редактору».",
        parse_mode="HTML",
    )
    await callback.answer("Отправлено студенту на подтверждение цены")


# ─── Отклонить заявку ─────────────────────────────────────────────────────────

# ВАЖНО: исключаем reject_wd_* и reject_refund_*, иначе этот хендлер (он
# зарегистрирован раньше) перехватывал бы отклонение вывода/возврата и
# отменял бы заказ с совпавшим номером. См. cb_reject_wd / cb_reject_refund.
@router.callback_query(
    F.data.startswith("reject_")
    & ~F.data.startswith("reject_wd_")
    & ~F.data.startswith("reject_refund_")
)
async def cb_reject_order(callback: CallbackQuery, user, bot: Bot) -> None:
    if not _is_admin(user):
        await callback.answer("Нет доступа.", show_alert=True)
        return

    order_id = int(callback.data.split("_")[-1])

    async with AsyncSessionLocal() as session:
        order = await get_order(session, order_id)
        if order is None:
            await callback.answer("Заказ не найден.", show_alert=True)
            return

        await update_order_status(session, order_id, OrderStatus.CANCELLED)

        # Уведомляем студента
        try:
            await bot.send_message(
                chat_id=order.user.tg_id,
                text=f"❌ <b>Заявка #{order_id} отклонена.</b>\n\n"
                     f"К сожалению, мы не можем принять эту заявку. "
                     f"Если есть вопросы — напишите нам.",
                parse_mode="HTML",
            )
        except Exception as e:
            logger.error(f"Не удалось уведомить студента: {e}")

    await callback.message.edit_text(
        callback.message.text + "\n\n❌ <b>ОТКЛОНЕНО</b>",
        parse_mode="HTML",
    )
    await callback.answer("Заявка отклонена.")


# ─── Изменить цену ────────────────────────────────────────────────────────────

@router.callback_query(F.data.startswith("change_price_"))
async def cb_change_price_start(callback: CallbackQuery, user, state: FSMContext) -> None:
    if not _is_admin(user):
        await callback.answer("Нет доступа.", show_alert=True)
        return

    order_id = int(callback.data.split("_")[-1])
    await state.update_data(changing_price_order_id=order_id)
    await state.set_state(AdminPriceStates.waiting_price)

    await callback.message.answer(
        f"✏️ Введи новую цену для заказа #{order_id} (в рублях, только цифры):"
    )
    await callback.answer()


@router.message(AdminPriceStates.waiting_price)
async def handle_new_price(message: Message, user, state: FSMContext, bot: Bot) -> None:
    if not _is_admin(user):
        return

    text = (message.text or "").strip().replace(",", ".").replace(" ", "")
    try:
        new_price = float(text)
        if new_price <= 0:
            raise ValueError
    except ValueError:
        await message.answer("Введи корректную цену (положительное число):")
        return

    data = await state.get_data()
    order_id = data.get("changing_price_order_id")
    await state.clear()

    async with AsyncSessionLocal() as session:
        order = await get_order(session, order_id)
        if order is None:
            await message.answer("Заказ не найден.")
            return

        await update_order_price(session, order_id, new_price)
        order = await get_order(session, order_id)  # перечитываем с новой ценой

        work_display = WORK_TYPE_NAMES.get(order.work_type or "drugoe", order.work_type or "—")
        comment_text = order.comment or "—"
        has_file = bool(order.files)
        file_info = "✅ загружен студентом" if has_file else "наш шаблон"

        card = (
            f"✏️ <b>Цена обновлена — заявка #{order_id}</b>\n\n"
            f"👤 Студент: @{order.user.username or '—'} ({order.user.full_name or '—'})\n\n"
            f"📚 <b>Тип:</b> {work_display}\n"
            f"📝 <b>Тема:</b> {order.topic}\n"
            f"🎓 <b>Предмет:</b> {order.subject}\n"
            f"📏 <b>Объём:</b> {order.pages} стр.\n"
            f"📅 <b>Дедлайн:</b> {order.deadline}\n"
            f"💬 <b>Комментарий:</b> {comment_text}\n"
            f"📎 <b>Шаблон:</b> {file_info}\n\n"
            f"💰 <b>Новая цена: {new_price:.0f} руб.</b>\n\n"
            f"Подтвердить заявку с новой ценой?"
        )

    await message.answer(card, reply_markup=admin_order_kb(order_id), parse_mode="HTML")


# ─── Скопировать промпт ───────────────────────────────────────────────────────

@router.callback_query(F.data.startswith("copy_prompt_"))
async def cb_copy_prompt(callback: CallbackQuery, user) -> None:
    if not _is_admin(user):
        await callback.answer("Нет доступа.", show_alert=True)
        return

    order_id = int(callback.data.split("_")[-1])

    async with AsyncSessionLocal() as session:
        order = await get_order(session, order_id)

    if order is None or not order.prompt_text:
        await callback.answer("Промпт не найден.", show_alert=True)
        return

    # Отправляем промпт отдельным сообщением — удобно для копирования
    await callback.message.answer(
        f"📋 <b>Промпт для Claude — заказ #{order_id}</b>\n\n"
        f"<code>{order.prompt_text}</code>",
        parse_mode="HTML",
    )
    await callback.answer("Промпт отправлен ↑")


# ─── Отправить PDF-шаблон оформления ─────────────────────────────────────────

@router.callback_query(F.data.startswith("send_template_"))
async def cb_send_template(callback: CallbackQuery, user, bot: Bot) -> None:
    if not _is_admin(user):
        await callback.answer("Нет доступа.", show_alert=True)
        return

    order_id = int(callback.data.split("_")[-1])

    async with AsyncSessionLocal() as session:
        order = await get_order(session, order_id)

    if order is None:
        await callback.answer("Заказ не найден.", show_alert=True)
        return

    pdf_name = _PDF_TEMPLATE_MAP.get(order.work_type or "")
    if not pdf_name:
        await callback.answer("Шаблон для этого типа работы не предусмотрен.", show_alert=True)
        return

    pdf_path = os.path.join(settings.TEMPLATES_DIR, pdf_name)
    if not os.path.isfile(pdf_path):
        await callback.answer("Файл шаблона не найден на сервере.", show_alert=True)
        return

    await callback.message.answer_document(
        document=FSInputFile(pdf_path),
        caption=f"📎 Шаблон оформления для заказа #{order_id} ({order.work_type})",
    )
    await callback.answer()


# ─── Просмотр заказа (из /admin_orders) ──────────────────────────────────────

@router.callback_query(F.data.startswith("view_order_"))
async def cb_view_order(callback: CallbackQuery, user) -> None:
    if not _is_admin(user):
        await callback.answer("Нет доступа.", show_alert=True)
        return

    order_id = int(callback.data.split("_")[-1])

    async with AsyncSessionLocal() as session:
        order = await get_order(session, order_id)

    if order is None:
        await callback.answer("Заказ не найден.", show_alert=True)
        return

    work_display = WORK_TYPE_NAMES.get(order.work_type or "drugoe", order.work_type or "—")
    comment_text = order.comment or "—"
    status_raw = order.status if isinstance(order.status, str) else order.status.value
    status_display = STATUS_NAMES.get(status_raw, status_raw)
    has_file = bool(order.files)
    file_info = "✅ загружен студентом" if has_file else "—"

    card = (
        f"📋 <b>Заказ #{order_id}</b>\n\n"
        f"👤 Студент: @{order.user.username or '—'} ({order.user.full_name or '—'})\n"
        f"📚 Тип: {work_display}\n"
        f"📝 Тема: {order.topic}\n"
        f"🎓 Предмет: {order.subject}\n"
        f"📏 Объём: {order.pages} стр.\n"
        f"📅 Дедлайн: {order.deadline}\n"
        f"💬 Комментарий: {comment_text}\n"
        f"📎 Шаблон: {file_info}\n"
        f"💰 Цена: {order.price or '—'} руб.\n"
        f"📊 Статус: {status_display}"
    )

    # Кнопки зависят от статуса:
    # PENDING → можно принять/отклонить/изменить цену
    # IN_WORK / EDITING / PREVIEW_SENT / AWAITING_PAYMENT → промпт + шаблон + отправить редактору
    # DONE / CANCELLED / REFUNDED → только информация (без кнопок действия)
    if status_raw == OrderStatus.PENDING.value:
        kb = admin_order_kb(order_id)
    elif status_raw in (
        OrderStatus.DONE.value,
        OrderStatus.CANCELLED.value,
        OrderStatus.REFUNDED.value,
    ):
        kb = None  # терминальный статус — действий нет
    else:
        kb = admin_after_accept_kb(order_id, order.work_type or "")

    await callback.message.answer(card, reply_markup=kb, parse_mode="HTML")
    await callback.answer()


async def _tg_retry(factory, attempts: int = 3):
    """Повтор отправки в Telegram при сетевых сбоях.

    Канал хостинга до api.telegram.org временами тормозит/отваливается —
    одиночный таймаут не повод считать отправку проваленной. Повторяем с паузой.
    """
    from aiogram.exceptions import TelegramNetworkError
    last_exc = None
    for n in range(1, attempts + 1):
        try:
            return await factory()
        except TelegramNetworkError as e:
            last_exc = e
            logger.warning(f"Telegram сетевой сбой (попытка {n}/{attempts}): {e}")
            if n < attempts:
                await asyncio.sleep(2 * n)
    raise last_exc  # type: ignore[misc]


# ─── Отправить редактору: начало FSM ─────────────────────────────────────────

@router.callback_query(F.data.startswith("send_editor_"))
async def cb_send_editor_start(callback: CallbackQuery, user, state: FSMContext) -> None:
    """Нажата кнопка «Отправить редактору» — начинаем сбор файлов."""
    if not _is_admin(user):
        await callback.answer("Нет доступа.", show_alert=True)
        return

    order_id = int(callback.data.split("_")[-1])
    await state.set_state(AdminSendToEditorStates.collecting)
    await state.update_data(send_order_id=order_id, send_files=[], send_comment="")

    await callback.message.answer(
        f"📤 <b>Отправка редактору — заказ #{order_id}</b>\n\n"
        f"Загружай файлы (любые: .docx, .pptx, .pdf, фото и т.д.) — по одному или сразу несколько.\n"
        f"Можешь также написать комментарий для редактора текстом.\n\n"
        f"Когда всё загрузишь — нажми <b>«✅ Отправить редактору»</b>.",
        reply_markup=admin_collect_files_kb(),
        parse_mode="HTML",
    )
    await callback.answer()


@router.message(AdminSendToEditorStates.collecting, F.document)
async def cb_send_editor_document(message: Message, state: FSMContext, bot: Bot) -> None:
    """Администратор загружает документ в режиме сбора."""
    doc: Document = message.document
    data = await state.get_data()
    files = data.get("send_files", [])
    files.append({"type": "document", "file_id": doc.file_id, "filename": doc.file_name or "файл"})
    await state.update_data(send_files=files)
    await message.answer(
        f"📎 Файл <b>{doc.file_name}</b> добавлен ({len(files)} шт.)\n"
        f"Загружай ещё или нажми <b>«✅ Отправить редактору»</b>.",
        parse_mode="HTML",
        reply_markup=admin_collect_files_kb(),
    )


@router.message(AdminSendToEditorStates.collecting, F.photo)
async def cb_send_editor_photo(message: Message, state: FSMContext) -> None:
    """Администратор загружает фото в режиме сбора."""
    photo = message.photo[-1]
    data = await state.get_data()
    files = data.get("send_files", [])
    files.append({"type": "photo", "file_id": photo.file_id, "filename": "фото.jpg"})
    await state.update_data(send_files=files)
    await message.answer(
        f"🖼 Фото добавлено ({len(files)} шт.)\n"
        f"Загружай ещё или нажми <b>«✅ Отправить редактору»</b>.",
        parse_mode="HTML",
        reply_markup=admin_collect_files_kb(),
    )


@router.message(AdminSendToEditorStates.collecting, F.text == "❌ Отмена")
async def cb_send_editor_cancel(message: Message, state: FSMContext) -> None:
    """Отмена отправки редактору."""
    await state.clear()
    from bot.keyboards.admin_kb import admin_main_kb
    await message.answer("Отправка отменена.", reply_markup=admin_main_kb())


@router.message(AdminSendToEditorStates.collecting, F.text == "✅ Отправить редактору")
async def cb_send_editor_confirm(message: Message, state: FSMContext, bot: Bot) -> None:
    """Администратор нажал «Отправить» — отправляем всё редактору."""
    data = await state.get_data()
    order_id = data.get("send_order_id")
    files = data.get("send_files", [])
    comment = data.get("send_comment", "").strip()
    await state.clear()

    from bot.keyboards.admin_kb import admin_main_kb

    if not files:
        await message.answer(
            "⚠️ Ты не добавил ни одного файла. Загрузи хотя бы один файл.",
            reply_markup=admin_collect_files_kb(),
        )
        # Восстанавливаем состояние
        await state.set_state(AdminSendToEditorStates.collecting)
        await state.update_data(send_order_id=order_id, send_files=[], send_comment=comment)
        return

    async with AsyncSessionLocal() as session:
        order = await get_order(session, order_id)
        if order is None:
            await message.answer("Заказ не найден.", reply_markup=admin_main_kb())
            return

        # Сохраняем файлы в БД и переводим статус
        os.makedirs(settings.UPLOADS_DIR, exist_ok=True)
        for f in files:
            local_path = os.path.join(settings.UPLOADS_DIR, f"order_{order_id}_draft_{f['filename']}")
            try:
                file_info = await bot.get_file(f["file_id"])
                await bot.download_file(file_info.file_path, local_path)
            except Exception:
                pass
            await save_file(
                session=session,
                order_id=order_id,
                file_type=FileType.DRAFT,
                tg_file_id=f["file_id"],
                filename=f["filename"],
                local_path=local_path,
            )

        await update_order_status(session, order_id, OrderStatus.EDITING)
        student_tg_id = order.user.tg_id
        student_name = order.user.full_name or order.user.username or "Студент"
        work_display = WORK_TYPE_NAMES.get(order.work_type or "drugoe", order.work_type or "—")

    await message.answer(
        f"✅ Отправляю редактору {len(files)} файл(ов) по заказу #{order_id}...",
        reply_markup=admin_main_kb(),
    )

    # Уведомляем студента
    try:
        await bot.send_message(
            chat_id=student_tg_id,
            text=f"🔄 <b>Заявка #{order_id} передана редактору</b>\n\n"
                 f"Работа проходит финальную проверку. Скоро пришлём превью!",
            parse_mode="HTML",
        )
    except Exception as e:
        logger.error(f"Не удалось уведомить студента: {e}")

    # Уведомляем редактора
    if not settings.EDITOR_TG_ID:
        await message.answer("⚠️ EDITOR_TG_ID не задан в .env — редактор не уведомлён!")
        return

    try:
        comment_line = f"\n💬 Комментарий: {comment}" if comment else ""
        brief_text = (
            f"📬 <b>Новое задание — заказ #{order_id}</b>\n\n"
            f"👤 Студент: {student_name}\n"
            f"📚 Тип: {work_display}\n"
            f"📝 Тема: {order.topic}\n"
            f"📏 Объём: {order.pages} стр.\n"
            f"📅 Дедлайн: {order.deadline}"
            f"{comment_line}\n\n"
            f"Файлы ({len(files)} шт.) — ниже. После правки нажми <b>«Готово»</b>."
        )
        await _tg_retry(lambda: bot.send_message(
            chat_id=settings.EDITOR_TG_ID,
            text=brief_text,
            parse_mode="HTML",
        ))
        # Отправляем каждый файл (с ретраями на сетевых сбоях)
        for i, f in enumerate(files):
            is_last = (i == len(files) - 1)
            kb = editor_done_kb(order_id) if is_last else None
            if f["type"] == "photo":
                await _tg_retry(lambda f=f, i=i, kb=kb: bot.send_photo(
                    chat_id=settings.EDITOR_TG_ID,
                    photo=f["file_id"],
                    caption=f"🖼 Файл {i+1}/{len(files)} — заказ #{order_id}",
                    reply_markup=kb,
                ))
            else:
                await _tg_retry(lambda f=f, i=i, kb=kb: bot.send_document(
                    chat_id=settings.EDITOR_TG_ID,
                    document=f["file_id"],
                    caption=f"📄 Файл {i+1}/{len(files)} — заказ #{order_id}",
                    reply_markup=kb,
                ))
        await message.answer("✅ Готово — редактор получил задание.", reply_markup=admin_main_kb())
    except Exception as e:
        logger.error(f"Не удалось уведомить редактора: {e}")
        await message.answer(
            f"⚠️ Сеть до Telegram сейчас нестабильна — не удалось доставить редактору ({e}).\n\n"
            f"Файлы и заказ #{order_id} уже сохранены (статус EDITING). "
            f"Попробуй ещё раз через минуту: открой заказ и снова нажми «Отправить редактору», "
            f"либо открой задание в веб-кабинете редактора.",
            reply_markup=admin_main_kb(),
        )


@router.message(AdminSendToEditorStates.collecting, F.text)
async def cb_send_editor_comment(message: Message, state: FSMContext) -> None:
    """Администратор написал текст — это комментарий для редактора."""
    data = await state.get_data()
    prev = data.get("send_comment", "")
    new_comment = (prev + "\n" + message.text).strip() if prev else message.text
    await state.update_data(send_comment=new_comment)
    await message.answer(
        f"💬 Комментарий сохранён.\nЗагружай файлы или нажми <b>«✅ Отправить редактору»</b>.",
        parse_mode="HTML",
        reply_markup=admin_collect_files_kb(),
    )


# ─── /admin_orders ────────────────────────────────────────────────────────────

@router.message(Command("admin_orders"))
@router.message(F.text == "📂 Все заказы")
async def cmd_admin_orders(message: Message, user) -> None:
    if not _is_admin(user):
        await message.answer("Нет доступа.")
        return

    async with AsyncSessionLocal() as session:
        orders = await get_all_orders(session, limit=50)

    active = [
        o for o in orders
        if (o.status if isinstance(o.status, str) else o.status.value)
           not in (OrderStatus.CANCELLED.value, OrderStatus.REFUNDED.value)
    ]

    if not active:
        await message.answer("Активных заявок пока нет.")
        return

    from bot.keyboards.admin_kb import admin_orders_kb
    await message.answer(
        f"📊 <b>Все заявки</b> (активные: {len(active)}):",
        reply_markup=admin_orders_kb(orders),
        parse_mode="HTML",
    )


# ─── Управление выводами ──────────────────────────────────────────────────────

async def _send_withdrawals_page(target, offset: int = 0) -> None:
    """Вспомогательная функция: отправляет страницу заявок на вывод"""
    from bot.keyboards.admin_kb import withdrawal_action_kb, withdrawal_page_kb

    async with AsyncSessionLocal() as session:
        requests = await get_pending_withdrawals(session, limit=10, offset=offset)
        total = await count_pending_withdrawals(session)

    if not requests:
        text = "💸 <b>Заявки на вывод</b>\n\nПока нет заявок."
        if hasattr(target, "message"):
            await target.message.answer(text, parse_mode="HTML")
        else:
            await target.answer(text, parse_mode="HTML")
        return

    # Отправляем каждую заявку отдельным сообщением с кнопками
    if hasattr(target, "message"):
        send_fn = target.message.answer
    else:
        send_fn = target.answer

    await send_fn(
        f"💸 <b>Заявки на вывод</b> (стр. {offset // 10 + 1}, всего: {total}):",
        parse_mode="HTML",
    )

    for req in requests:
        user = req.user
        masked = req.card_details[:4] + " **** **** " + req.card_details[-4:] if len(req.card_details) >= 8 else req.card_details
        created = req.created_at.strftime("%d.%m.%Y %H:%M")
        card_text = (
            f"💸 <b>Заявка #{req.id}</b>\n\n"
            f"👤 @{user.username or '—'} ({user.full_name or '—'})\n"
            f"💰 Сумма: <b>{req.amount:.0f} руб.</b>\n"
            f"💳 Карта: <code>{masked}</code>\n"
            f"📅 Создана: {created}"
        )
        await send_fn(card_text, reply_markup=withdrawal_action_kb(req.id), parse_mode="HTML")

    # Пагинация
    page_kb = withdrawal_page_kb(offset, total)
    if page_kb.inline_keyboard:
        await send_fn("Навигация:", reply_markup=page_kb)


@router.message(Command("admin_withdrawals"))
@router.message(F.text == "💸 Выводы")
async def cmd_admin_withdrawals(message: Message, user) -> None:
    if not _is_admin(user):
        await message.answer("Нет доступа.")
        return
    await _send_withdrawals_page(message, offset=0)


@router.callback_query(F.data.startswith("wd_page_"))
async def cb_wd_page(callback: CallbackQuery, user) -> None:
    if not _is_admin(user):
        await callback.answer("Нет доступа.", show_alert=True)
        return
    offset = int(callback.data.split("_")[-1])
    await _send_withdrawals_page(callback, offset=offset)
    await callback.answer()


@router.callback_query(F.data.startswith("approve_wd_"))
async def cb_approve_wd(callback: CallbackQuery, user, bot: Bot) -> None:
    if not _is_admin(user):
        await callback.answer("Нет доступа.", show_alert=True)
        return

    wd_id = int(callback.data.split("_")[-1])

    async with AsyncSessionLocal() as session:
        req = await update_withdrawal_status(session, wd_id, WithdrawalStatus.APPROVED)

    if req is None:
        await callback.answer("Заявка не найдена или уже обработана.", show_alert=True)
        return

    await callback.message.edit_reply_markup(reply_markup=None)
    await callback.message.edit_text(
        callback.message.text + "\n\n✅ <b>ОДОБРЕНО</b>",
        parse_mode="HTML",
    )

    # Уведомляем студента
    async with AsyncSessionLocal() as session:
        from db.models import User as UserModel
        from sqlalchemy import select
        r = await session.execute(
            select(UserModel).where(UserModel.id == req.user_id)
        )
        student = r.scalar_one_or_none()

    if student:
        try:
            await bot.send_message(
                chat_id=student.tg_id,
                text=(
                    f"✅ <b>Заявка на вывод #{req.id} одобрена!</b>\n\n"
                    f"Сумма <b>{req.amount:.0f} руб.</b> будет переведена на вашу карту "
                    f"в течение 24 часов."
                ),
                parse_mode="HTML",
            )
        except Exception as e:
            logger.error(f"Ошибка уведомления студента о выводе: {e}")

    await callback.answer("Одобрено!")


@router.callback_query(F.data.startswith("reject_wd_"))
async def cb_reject_wd(callback: CallbackQuery, user, bot: Bot) -> None:
    if not _is_admin(user):
        await callback.answer("Нет доступа.", show_alert=True)
        return

    wd_id = int(callback.data.split("_")[-1])

    async with AsyncSessionLocal() as session:
        req = await update_withdrawal_status(session, wd_id, WithdrawalStatus.REJECTED)

    if req is None:
        await callback.answer("Заявка не найдена или уже обработана.", show_alert=True)
        return

    await callback.message.edit_reply_markup(reply_markup=None)
    await callback.message.edit_text(
        callback.message.text + "\n\n❌ <b>ОТКЛОНЕНО</b> (баланс возвращён)",
        parse_mode="HTML",
    )

    # Уведомляем студента
    async with AsyncSessionLocal() as session:
        from db.models import User as UserModel
        from sqlalchemy import select
        r = await session.execute(
            select(UserModel).where(UserModel.id == req.user_id)
        )
        student = r.scalar_one_or_none()

    if student:
        try:
            await bot.send_message(
                chat_id=student.tg_id,
                text=(
                    f"❌ <b>Заявка на вывод #{req.id} отклонена.</b>\n\n"
                    f"Сумма <b>{req.amount:.0f} руб.</b> возвращена на ваш реферальный баланс."
                ),
                parse_mode="HTML",
            )
        except Exception as e:
            logger.error(f"Ошибка уведомления студента об отклонении вывода: {e}")

    await callback.answer("Отклонено, баланс возвращён.")


# ─── Аналитика ────────────────────────────────────────────────────────────────

@router.message(Command("admin_stats"))
@router.message(F.text == "📊 Аналитика")
async def cmd_admin_stats(message: Message, user) -> None:
    if not _is_admin(user):
        await message.answer("Нет доступа.")
        return

    from datetime import datetime
    async with AsyncSessionLocal() as session:
        stats = await get_admin_analytics(session)

    os_ = stats["orders_by_status"]
    from db.models import OrderStatus as OS

    top_ref_lines = []
    for i, u in enumerate(stats["top_referrers"], 1):
        name = f"@{u.username}" if u.username else u.full_name or f"id{u.tg_id}"
        top_ref_lines.append(f"  {i}. {name} — <b>{u.ref_balance:.0f} руб.</b>")
    top_ref_text = "\n".join(top_ref_lines) if top_ref_lines else "  Нет данных"

    now_str = datetime.utcnow().strftime("%d.%m.%Y %H:%M")

    text = (
        f"📊 <b>Дашборд — {now_str} UTC</b>\n\n"
        f"👤 <b>Пользователи:</b>\n"
        f"  Студентов: <b>{stats['total_users']}</b>\n"
        f"  Новых: сегодня <b>{stats['new_today']}</b> / неделя <b>{stats['new_week']}</b> / месяц <b>{stats['new_month']}</b>\n\n"
        f"📦 <b>Заказы:</b>\n"
        f"  Всего: <b>{stats['total_orders']}</b>\n"
        f"  Ожидают: <b>{os_.get(OS.PENDING.value, 0)}</b> | "
        f"В работе: <b>{os_.get(OS.IN_WORK.value, 0)}</b> | "
        f"У редактора: <b>{os_.get(OS.EDITING.value, 0)}</b>\n"
        f"  Превью: <b>{os_.get(OS.PREVIEW_SENT.value, 0)}</b> | "
        f"Оплата: <b>{os_.get(OS.AWAITING_PAYMENT.value, 0)}</b> | "
        f"Готово: <b>{os_.get(OS.DONE.value, 0)}</b>\n\n"
        f"💰 <b>Выручка:</b>\n"
        f"  Всего: <b>{stats['total_revenue']:.0f} руб.</b>\n"
        f"  За месяц: <b>{stats['revenue_month']:.0f} руб.</b>\n\n"
        f"💸 <b>Выводы на рассмотрении:</b> <b>{stats['pending_wd_count']}</b> "
        f"(сумма: <b>{stats['pending_wd_sum']:.0f} руб.</b>)\n\n"
        f"🏆 <b>Топ рефереров по балансу:</b>\n{top_ref_text}"
    )

    await message.answer(text, parse_mode="HTML")


# ─── Кнопки главного меню админа ─────────────────────────────────────────────

@router.message(F.text == "🔗 Рефералы")
async def cmd_admin_referrals(message: Message, user) -> None:
    """Панель рефералов: кому и сколько нужно заплатить"""
    if not _is_admin(user):
        return

    from db.crud import get_all_referral_debtors
    from db.models import BonusType as BT

    async with AsyncSessionLocal() as session:
        debtors = await get_all_referral_debtors(session)

    if not debtors:
        await message.answer(
            "🔗 <b>Рефералы</b>\n\nНет пользователей с начисленными бонусами.",
            parse_mode="HTML",
        )
        return

    total_debt = sum(d["balance"] for d in debtors)
    lines = [f"🔗 <b>Реферальный долг — {total_debt:.0f} руб. всего</b>\n"]

    for d in debtors:
        u = d["user"]
        name = f"@{u.username}" if u.username else (u.full_name or f"id{u.tg_id}")
        lines.append(
            f"👤 <b>{name}</b> — должно: <b>{d['balance']:.0f} руб.</b>\n"
            f"   Приглашено: {d['invited_count']} чел."
        )
        for b in d["recent_bonuses"]:
            btype = "первый заказ" if b.bonus_type == BT.FIRST_ORDER.value else "повторный"
            lines.append(f"   +{b.amount:.0f} руб. ({btype}, заказ #{b.order_id})")
        lines.append("")

    lines.append("💡 Для выплаты одобри заявку через <b>💸 Выводы</b>.")

    await message.answer("\n".join(lines), parse_mode="HTML")


@router.message(F.text == "📋 Заявки")
async def cmd_admin_pending(message: Message, user) -> None:
    """Быстрый доступ: заявки в статусе PENDING"""
    if not _is_admin(user):
        return
    async with AsyncSessionLocal() as session:
        from db.crud import get_orders_by_status
        orders = await get_orders_by_status(session, OrderStatus.PENDING)

    if not orders:
        await message.answer("Нет заявок, ожидающих рассмотрения.")
        return

    from bot.keyboards.admin_kb import admin_orders_kb
    await message.answer(
        f"🟡 <b>Заявки на рассмотрении</b> ({len(orders)}):",
        reply_markup=admin_orders_kb(orders),
        parse_mode="HTML",
    )


# ─── Управление возвратами (Refunds) ─────────────────────────────────────────

@router.message(Command("refund_list"))
@router.message(F.text == "📨 Возвраты")
async def cmd_admin_refunds(message: Message, user) -> None:
    """Список заявок на возврат на рассмотрении."""
    if not _is_admin(user):
        await message.answer("Нет доступа.")
        return

    from db.crud import get_pending_refund_requests, count_pending_refund_requests
    from bot.keyboards.admin_kb import refund_action_kb

    async with AsyncSessionLocal() as session:
        reqs = await get_pending_refund_requests(session, limit=20)
        total = await count_pending_refund_requests(session)

    if not reqs:
        await message.answer("📨 <b>Возвраты</b>\n\nПока нет ожидающих заявок.", parse_mode="HTML")
        return

    await message.answer(
        f"📨 <b>Заявки на возврат</b> (ожидают: {total}):",
        parse_mode="HTML",
    )

    for req in reqs:
        u = req.user
        o = req.order
        name = f"@{u.username}" if u.username else (u.full_name or f"id{u.tg_id}")
        created = req.created_at.strftime("%d.%m.%Y %H:%M")
        price = f"{o.price:.0f} ₽" if o and o.price else "—"
        text = (
            f"📨 <b>Заявка #{req.id}</b>\n\n"
            f"👤 Студент: {name}\n"
            f"📦 Заказ: <b>#{req.order_id}</b> · {price}\n"
            f"📅 Подана: {created}\n\n"
            f"<b>Причина:</b>\n<i>{req.reason}</i>"
        )
        await message.answer(text, reply_markup=refund_action_kb(req.id), parse_mode="HTML")


@router.callback_query(F.data.startswith("approve_refund_"))
async def cb_approve_refund(callback: CallbackQuery, user, bot: Bot) -> None:
    """Админ одобрил возврат → выполняем через API ЮКассы."""
    if not _is_admin(user):
        await callback.answer("Нет доступа.", show_alert=True)
        return

    refund_id = int(callback.data.split("_")[-1])

    from db.crud import get_refund_request, update_refund_status
    from db.models import PaymentStatus
    from services.payment import refund_yukassa_payment

    async with AsyncSessionLocal() as session:
        req = await get_refund_request(session, refund_id)

    if req is None:
        await callback.answer("Заявка не найдена.", show_alert=True)
        return

    current_status = req.status.value if hasattr(req.status, "value") else req.status
    if current_status != RefundStatus.PENDING.value:
        await callback.answer("Заявка уже обработана.", show_alert=True)
        return

    # Ищем PAID-платёж у заказа
    paid_payment = None
    for p in req.order.payments:
        p_status = p.status.value if hasattr(p.status, "value") else p.status
        if p_status == PaymentStatus.PAID.value:
            paid_payment = p
            break

    if paid_payment is None or not paid_payment.payment_id:
        await callback.answer("Платёж не найден.", show_alert=True)
        return

    await callback.message.edit_reply_markup(reply_markup=None)
    await callback.message.answer(f"⏳ Обрабатываю возврат заявки #{refund_id}...")

    # Робокасса не имеет публичного API возвратов — нужно сделать вручную через ЛК.
    # Просто фиксируем одобрение в БД и просим админа сделать возврат руками.
    yukassa_refund_id = None
    await callback.message.answer(
        f"⚠️ <b>Возврат нужно сделать вручную в Робокассе:</b>\n\n"
        f"1. Зайдите в личный кабинет Робокассы → раздел <b>«Возвраты»</b>\n"
        f"   https://partner.robokassa.ru/Cancel\n"
        f"2. Найдите платёж по InvId = <b>{req.order_id}</b> "
        f"(сумма {paid_payment.amount:.0f} ₽)\n"
        f"3. Оформите возврат через интерфейс Робокассы\n\n"
        f"После этого студент получит деньги на карту в течение 1–10 рабочих дней. "
        f"Я уже отметил заявку как APPROVED в боте — продолжаем.",
        parse_mode="HTML",
    )
    logger.info(f"Refund #{refund_id} помечен APPROVED, требуется ручной возврат в Робокассе")

    # Сохраняем результат в БД
    async with AsyncSessionLocal() as session:
        updated = await update_refund_status(
            session, refund_id, RefundStatus.APPROVED,
            yukassa_refund_id=yukassa_refund_id,
        )

    await callback.message.answer(
        f"✅ Возврат #{refund_id} одобрен. Заказ #{req.order_id} переведён в REFUNDED."
    )

    # Уведомление студенту
    try:
        await bot.send_message(
            chat_id=req.user.tg_id,
            text=(
                f"✅ <b>Возврат по заказу #{req.order_id} одобрен!</b>\n\n"
                f"Средства будут зачислены на карту в течение 1–10 рабочих дней "
                f"(зависит от вашего банка)."
            ),
            parse_mode="HTML",
        )
    except Exception as e:
        logger.error(f"Не удалось уведомить студента о возврате: {e}")

    await callback.answer("Возврат одобрен!")


@router.callback_query(F.data.startswith("reject_refund_"))
async def cb_reject_refund(callback: CallbackQuery, user, state: FSMContext) -> None:
    """Админ отклоняет возврат — спрашиваем причину."""
    if not _is_admin(user):
        await callback.answer("Нет доступа.", show_alert=True)
        return

    refund_id = int(callback.data.split("_")[-1])
    from bot.states.order import AdminRefundStates

    await state.set_state(AdminRefundStates.waiting_rejection_reason)
    await state.update_data(reject_refund_id=refund_id)

    await callback.message.edit_reply_markup(reply_markup=None)
    await callback.message.answer(
        f"📝 Введите причину отклонения заявки #{refund_id}:\n\n"
        f"Эта причина будет передана студенту.\n"
        f"Отмена: /cancel"
    )
    await callback.answer()


@router.message(AdminRefundStates.waiting_rejection_reason, F.text)
async def admin_reject_refund_reason_handler(message: Message, state: FSMContext, user, bot: Bot) -> None:
    """Обработка ввода причины отклонения возврата админом."""
    if not _is_admin(user):
        return

    text = (message.text or "").strip()
    if text.startswith("/cancel"):
        await state.clear()
        await message.answer("Отклонение отменено.")
        return
    if len(text) < 5:
        await message.answer("Причина слишком короткая, опишите подробнее.")
        return

    data = await state.get_data()
    refund_id = data.get("reject_refund_id")
    await state.clear()

    from db.crud import get_refund_request, update_refund_status
    async with AsyncSessionLocal() as session:
        req = await get_refund_request(session, refund_id)
        if req is None:
            await message.answer("Заявка не найдена.")
            return
        await update_refund_status(
            session, refund_id, RefundStatus.REJECTED, rejection_reason=text,
        )

    await message.answer(f"❌ Возврат #{refund_id} отклонён.")

    # Уведомление студенту
    try:
        await bot.send_message(
            chat_id=req.user.tg_id,
            text=(
                f"❌ <b>Возврат по заказу #{req.order_id} отклонён</b>\n\n"
                f"<b>Причина:</b>\n<i>{text}</i>\n\n"
                f"Если не согласны — напишите на {settings.SUPPORT_EMAIL}."
            ),
            parse_mode="HTML",
        )
    except Exception as e:
        logger.error(f"Не удалось уведомить студента об отклонении возврата: {e}")


# ─── Торг: ответ админа на контрпредложение студента ─────────────────────────

@router.callback_query(F.data.startswith("admin_negot_accept_"))
async def cb_admin_negot_accept(callback: CallbackQuery, user, bot: Bot) -> None:
    """Админ согласился на предложенную студентом цену."""
    if not _is_admin(user):
        await callback.answer("Нет доступа.", show_alert=True)
        return

    order_id = int(callback.data.removeprefix("admin_negot_accept_"))

    # Парсим offered_price из текста сообщения админа
    msg_text = callback.message.text or ""
    import re
    m = re.search(r"Предложение студента:\s*([\d\s]+)\s*₽", msg_text)
    if not m:
        await callback.answer("Не удалось распознать предложенную цену", show_alert=True)
        return
    new_price = int(m.group(1).replace(" ", ""))

    async with AsyncSessionLocal() as session:
        order = await get_order(session, order_id)
        if order is None:
            await callback.answer("Заказ не найден.", show_alert=True)
            return
        await update_order_price(session, order_id, float(new_price))
        await update_order_status(session, order_id, OrderStatus.IN_WORK)
        student_tg_id = order.user.tg_id

    await callback.message.edit_text(
        msg_text + f"\n\n✅ <b>Согласован: {new_price} ₽</b>",
        parse_mode="HTML",
    )
    await callback.answer("Цена согласована!")

    # Уведомление студенту
    try:
        await bot.send_message(
            chat_id=student_tg_id,
            text=(
                f"✅ <b>Заказ #{order_id} — цена согласована!</b>\n\n"
                f"Финальная цена: <b>{new_price} ₽</b>\n\n"
                f"Работа уже начата. Ожидайте превью."
            ),
            parse_mode="HTML",
        )
    except Exception as e:
        logger.error(f"Не удалось уведомить студента о согласовании цены #{order_id}: {e}")


@router.callback_query(F.data.startswith("admin_negot_counter_"))
async def cb_admin_negot_counter_start(callback: CallbackQuery, user, state: FSMContext) -> None:
    """Админ хочет предложить свою цену в ответ."""
    if not _is_admin(user):
        await callback.answer("Нет доступа.", show_alert=True)
        return

    order_id = int(callback.data.removeprefix("admin_negot_counter_"))
    await state.set_state(AdminNegotiationStates.waiting_counter)
    await state.update_data(negot_order_id=order_id, original_msg_text=callback.message.text or "")

    await callback.answer()
    await callback.message.answer(
        f"💬 Напишите свою цену для заказа #{order_id} (только число, например <code>500</code>).\n"
        f"Для отмены — /cancel",
        parse_mode="HTML",
    )


@router.message(AdminNegotiationStates.waiting_counter, F.text == "/cancel")
async def cb_admin_negot_cancel(message: Message, state: FSMContext) -> None:
    await state.clear()
    await message.answer("Контрпредложение отменено.")


@router.message(AdminNegotiationStates.waiting_counter, F.text)
async def cb_admin_negot_counter_submit(message: Message, user, state: FSMContext, bot: Bot) -> None:
    """Админ прислал свою контрцену — отправляем студенту."""
    text = (message.text or "").strip().replace(" ", "").replace("₽", "").replace("руб", "")
    try:
        counter_price = int(float(text))
    except (ValueError, TypeError):
        await message.answer("Введите цену числом.")
        return

    if counter_price <= 0 or counter_price > 1_000_000:
        await message.answer("Цена должна быть от 1 до 1 000 000 ₽.")
        return

    data = await state.get_data()
    order_id = data.get("negot_order_id")
    await state.clear()

    if not order_id:
        await message.answer("Ошибка состояния.")
        return

    # Обновляем цену в БД (это новая «текущая цена» для следующего раунда торга)
    async with AsyncSessionLocal() as session:
        order = await get_order(session, order_id)
        if order is None:
            await message.answer("Заказ не найден.")
            return
        await update_order_price(session, order_id, float(counter_price))
        student_tg_id = order.user.tg_id

    # Уведомление админу
    await message.answer(f"✅ Отправлено студенту: <b>{counter_price} ₽</b>.", parse_mode="HTML")

    # Уведомление студенту с теми же 3 кнопками
    try:
        await bot.send_message(
            chat_id=student_tg_id,
            text=(
                f"💬 <b>Встречное предложение по заказу #{order_id}</b>\n\n"
                f"Администратор предлагает цену: <b>{counter_price} ₽</b>\n\n"
                f"Согласны?"
            ),
            parse_mode="HTML",
            reply_markup=price_confirm_kb(order_id),
        )
    except Exception as e:
        logger.error(f"Не удалось отправить контрпредложение студенту #{order_id}: {e}")


@router.callback_query(F.data.startswith("admin_negot_reject_"))
async def cb_admin_negot_reject(callback: CallbackQuery, user, bot: Bot) -> None:
    """Админ отклонил заявку после торга."""
    if not _is_admin(user):
        await callback.answer("Нет доступа.", show_alert=True)
        return

    order_id = int(callback.data.removeprefix("admin_negot_reject_"))

    async with AsyncSessionLocal() as session:
        order = await get_order(session, order_id)
        if order is None:
            await callback.answer("Заказ не найден.", show_alert=True)
            return
        await update_order_status(session, order_id, OrderStatus.CANCELLED)
        student_tg_id = order.user.tg_id

    await callback.message.edit_text(
        (callback.message.text or "") + "\n\n❌ <b>Отклонено админом</b>",
        parse_mode="HTML",
    )
    await callback.answer("Заявка отклонена.")

    try:
        await bot.send_message(
            chat_id=student_tg_id,
            text=(
                f"😔 <b>Заказ #{order_id} отклонён</b>\n\n"
                f"К сожалению, мы не смогли договориться о цене. "
                f"Создайте новую заявку, если измените условия."
            ),
            parse_mode="HTML",
        )
    except Exception as e:
        logger.error(f"Не удалось уведомить студента об отклонении #{order_id}: {e}")
