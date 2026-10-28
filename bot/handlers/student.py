"""
Хендлеры для студентов:
- /start — приветствие и главное меню
- /new_order — запуск FSM создания заявки
- /my_orders — история заказов
- FSM шаги: тип работы → тема → предмет → страницы → дедлайн → комментарий → файл → подтверждение
"""
import os
import logging
from datetime import datetime
from aiogram import Router, F, Bot
from aiogram.types import Message, CallbackQuery, Document
from aiogram.filters import CommandStart, Command
from aiogram.fsm.context import FSMContext

from config import settings
from db.models import User, OrderStatus, FileType
from db.session import AsyncSessionLocal
from db.crud import (
    create_order, update_order_status, save_file, get_user_orders, get_order,
    set_referrer, get_referral_stats, create_withdrawal_request, get_last_withdrawal_request,
    set_user_consent, cancel_order_by_student,
    get_user_paid_orders, create_refund_request,
    request_revision, MAX_REVISIONS,
)
from services.prompt_builder import build_prompt
from services.template_selector import get_template_path
from bot.states.order import (
    OrderStates, WithdrawalStates, KonspektStates,
    RefundStates, SupportStates, RevisionStates, PreviewReworkStates,
    PriceNegotiationStates,
)
from bot.keyboards.student_kb import (
    work_type_kb, skip_kb, main_menu_kb, remove_kb, WORK_TYPES, WORK_TYPE_NAMES,
    nav_kb, skip_nav_kb,
    konspekt_source_kb, konspekt_materials_kb, KONSPEKT_SOURCE_TYPES,
    docs_kb, consent_kb, cancel_order_kb, cancel_order_confirm_kb,
    refund_order_select_kb, revision_request_kb, preview_actions_kb,
    preview_payment_kb, DOC_URLS,
)
from bot.keyboards.admin_kb import admin_order_kb

logger = logging.getLogger(__name__)
router = Router()

# Статусы заказа на русском для отображения
STATUS_DISPLAY = {
    OrderStatus.FILLING.value:          "✏️ Заполняется",
    OrderStatus.PENDING.value:          "🟡 На рассмотрении",
    OrderStatus.IN_WORK.value:          "🟠 В работе",
    OrderStatus.EDITING.value:          "🔴 На проверке у редактора",
    OrderStatus.PREVIEW_SENT.value:     "🔵 Превью отправлено",
    OrderStatus.AWAITING_PAYMENT.value: "💳 Ожидает оплаты",
    OrderStatus.DONE.value:             "✅ Выполнен",
    OrderStatus.CANCELLED.value:        "❌ Отменён",
    OrderStatus.REFUNDED.value:         "💸 Деньги возвращены",
}


# ─── /start ──────────────────────────────────────────────────────────────────

@router.message(CommandStart())
async def cmd_start(message: Message, user: User, state: FSMContext) -> None:
    # Сбрасываем FSM-состояние — бот никогда не должен застрять
    await state.clear()

    # Обрабатываем реферальный параметр: /start ref_123456789
    args = message.text.split(maxsplit=1)
    if len(args) > 1 and args[1].startswith("ref_"):
        referrer_tg_id_str = args[1][4:]  # убираем "ref_"
        if referrer_tg_id_str.isdigit():
            referrer_tg_id = int(referrer_tg_id_str)
            if referrer_tg_id != message.from_user.id:
                async with AsyncSessionLocal() as session:
                    saved = await set_referrer(session, user.id, referrer_tg_id)
                if saved:
                    await message.answer("🎉 Вы зарегистрированы по реферальной ссылке!")

    if user.role == "admin":
        msg_text = (
            f"👋 Привет, администратор <b>{message.from_user.first_name}</b>!\n\n"
            f"Здесь вы можете обрабатывать новые заявки и управлять заказами."
        )
    elif user.role == "editor":
        msg_text = (
            f"👋 Привет, редактор <b>{message.from_user.first_name}</b>!\n\n"
            f"Здесь вы можете брать заказы в работу и проверять документы."
        )
    else:
        msg_text = (
            f"👋 Привет, <b>{message.from_user.first_name}</b>!\n\n"
            f"Помогаем студентам с курсовыми, рефератами, статьями "
            f"и презентациями. Быстро, по делу, с чеком.\n\n"
            f"• 📋 <b>Новая заявка</b> — оформить заказ\n"
            f"• 📂 <b>Мои заказы</b> — статусы и история\n"
            f"• 🔗 <b>Реферальная программа</b> — приглашай друзей\n"
            f"• 📄 <b>Документы</b> — оферта и политика\n"
            f"• ℹ️ <b>Помощь</b> — список команд\n\n"
            f"Жми <b>«Новая заявка»</b> — это займёт пару минут."
        )

    if user.role == "admin":
        from bot.keyboards.admin_kb import admin_main_kb
        kb = admin_main_kb()
    elif user.role == "editor":
        from bot.keyboards.admin_kb import editor_main_kb
        kb = editor_main_kb()
    else:
        kb = main_menu_kb(user.role)

    await message.answer(msg_text, reply_markup=kb, parse_mode="HTML")

    # Для студента: если ещё не давал согласие — предложим принять оферту
    if user.role == "student" and user.consented_at is None:
        await message.answer(
            "🤝 <b>Один момент перед заказом</b>\n\n"
            "Это формальность — кнопки ниже открывают документы сервиса. "
            "Нажми <b>«Согласен, продолжить»</b>, чтобы перейти к оформлению заказа.",
            reply_markup=consent_kb(),
            parse_mode="HTML",
        )


# ─── Хелпер: проверка согласия ───────────────────────────────────────────────

async def _require_consent(message: Message, user: User) -> bool:
    """
    Проверяет согласие пользователя с офертой.
    Если согласия нет — отправляет сообщение с просьбой принять и возвращает False.
    """
    if user.role != "student":
        return True  # админам/редакторам не нужно
    if user.consented_at is not None:
        return True
    await message.answer(
        "⚠️ <b>Чтобы продолжить, примите оферту</b>\n\n"
        "Перед использованием платных функций нужно один раз согласиться "
        "с публичной офертой и политикой конфиденциальности.",
        reply_markup=consent_kb(),
        parse_mode="HTML",
    )
    return False


# ─── Согласие с офертой (callback) ───────────────────────────────────────────

@router.callback_query(F.data == "consent_accept")
async def cb_consent_accept(callback: CallbackQuery, user: User) -> None:
    if user.role != "student":
        await callback.answer("Нет необходимости — у вас служебная роль.", show_alert=True)
        return
    if user.consented_at is not None:
        await callback.answer("Согласие уже было принято ранее.", show_alert=True)
        return

    async with AsyncSessionLocal() as session:
        await set_user_consent(session, user.tg_id)

    await callback.message.edit_reply_markup(reply_markup=None)
    await callback.message.answer(
        "✅ <b>Спасибо!</b>\n\n"
        "Согласие принято. Теперь можно оформить заявку — нажмите <b>«📋 Новая заявка»</b>.",
        parse_mode="HTML",
    )
    await callback.answer("Принято")


# ─── /docs — документы ───────────────────────────────────────────────────────

@router.message(Command("docs"))
@router.message(F.text == "📄 Документы")
async def cmd_docs(message: Message) -> None:
    await message.answer(
        "📄 <b>Документы сервиса</b>\n\n"
        "Все юридические документы доступны на нашем сайте:",
        reply_markup=docs_kb(),
        parse_mode="HTML",
    )


@router.message(Command("oferta"))
async def cmd_oferta(message: Message) -> None:
    await message.answer(
        f"📄 <b>Публичная оферта</b>\n\n{DOC_URLS['oferta']}",
        parse_mode="HTML",
        disable_web_page_preview=False,
    )


@router.message(Command("privacy"))
async def cmd_privacy(message: Message) -> None:
    await message.answer(
        f"🔒 <b>Политика конфиденциальности</b>\n\n{DOC_URLS['privacy']}",
        parse_mode="HTML",
        disable_web_page_preview=False,
    )


@router.message(Command("agreement"))
@router.message(Command("terms"))
async def cmd_agreement(message: Message) -> None:
    await message.answer(
        f"📜 <b>Пользовательское соглашение</b>\n\n{DOC_URLS['agreement']}",
        parse_mode="HTML",
        disable_web_page_preview=False,
    )


# ─── /help ───────────────────────────────────────────────────────────────────

@router.message(Command("help"))
@router.message(F.text == "ℹ️ Помощь")
async def cmd_help(message: Message, user: User) -> None:
    if user.role == "admin":
        text = (
            "ℹ️ <b>Команды администратора</b>\n\n"
            "• /admin_orders — все заявки\n"
            "• /admin_withdrawals — выводы средств\n"
            "• /admin_stats — аналитика\n"
            "• /refund_list — заявки на возврат\n"
            "• /start — главное меню"
        )
    elif user.role == "editor":
        text = (
            "ℹ️ <b>Команды редактора</b>\n\n"
            "• /start — главное меню\n"
            "• 📋 <b>Мои задания</b> — список активных заказов в работе"
        )
    else:
        text = (
            "ℹ️ <b>Команды бота</b>\n\n"
            "<b>Основные:</b>\n"
            "• /new_order — оформить новый заказ\n"
            "• /my_orders — мои заказы и их статусы\n"
            "• /cancel_order &lt;id&gt; — отменить заявку (до приёма админом)\n"
            "• /refund — подать заявку на возврат после оплаты\n\n"
            "<b>Сервисное:</b>\n"
            "• /email — указать email для чеков\n"
            "• /ref — реферальная программа и ссылка-приглашение\n"
            "• /balance — реферальный баланс\n"
            "• /docs — все юридические документы\n"
            "• /support — связь с поддержкой\n"
            "• /help — эта справка\n\n"
            f"<b>Связь:</b> {settings.PUBLIC_URL} · {settings.SUPPORT_EMAIL}"
        )
    await message.answer(text, parse_mode="HTML")


# ─── /support ────────────────────────────────────────────────────────────────

@router.message(Command("support"))
async def cmd_support(message: Message, state: FSMContext) -> None:
    await state.set_state(SupportStates.waiting_message)
    await message.answer(
        "💬 <b>Связь с поддержкой</b>\n\n"
        "Опишите ваш вопрос или проблему одним сообщением — мы получим уведомление "
        "и свяжемся с вами как только сможем.\n\n"
        f"Также вы всегда можете написать на email: <b>{settings.SUPPORT_EMAIL}</b>\n\n"
        "Отмена: /cancel",
        parse_mode="HTML",
    )


@router.message(SupportStates.waiting_message, F.text)
async def support_message_received(message: Message, state: FSMContext, user: User, bot: Bot) -> None:
    text = (message.text or "").strip()
    if text.startswith("/cancel"):
        await state.clear()
        await message.answer("Отменено.")
        return
    if len(text) < 5:
        await message.answer("Сообщение слишком короткое. Опишите проблему подробнее.")
        return

    await state.clear()

    # Уведомляем админа
    name = f"@{user.username}" if user.username else (user.full_name or f"id{user.tg_id}")
    try:
        await bot.send_message(
            chat_id=settings.ADMIN_TG_ID,
            text=(
                f"💬 <b>Обращение в поддержку</b>\n\n"
                f"От: {name} (tg_id={user.tg_id})\n\n"
                f"<i>{text}</i>"
            ),
            parse_mode="HTML",
        )
    except Exception as e:
        logger.error(f"Не удалось уведомить админа об обращении: {e}")

    await message.answer(
        "✅ Спасибо! Сообщение отправлено в поддержку. Мы свяжемся с вами в ближайшее время.",
        reply_markup=main_menu_kb(user.role),
    )


# ─── /new_order + кнопка «Новая заявка» ──────────────────────────────────────

@router.message(Command("new_order"))
@router.message(F.text == "📋 Новая заявка")
@router.message(F.text == "🔄 Новая заявка")
async def cmd_new_order(message: Message, state: FSMContext, user: User) -> None:
    # Проверка согласия с офертой — только для студентов
    if not await _require_consent(message, user):
        return

    await state.clear()
    await message.answer(
        "📋 <b>Новая заявка</b>\n\n"
        "Шаг 1 из 7 — выбери <b>тип работы</b>:",
        reply_markup=work_type_kb(),
        parse_mode="HTML",
    )
    await state.set_state(OrderStates.work_type)


# ─── Шаг 1: тип работы ───────────────────────────────────────────────────────

@router.message(OrderStates.work_type)
async def step_work_type(message: Message, state: FSMContext, user: User) -> None:
    text = message.text or ""

    if text == "◀️ Назад":
        await state.clear()
        await message.answer("Главное меню:", reply_markup=main_menu_kb(user.role))
        return

    if text not in WORK_TYPES:
        await message.answer(
            "Пожалуйста, выбери тип работы из кнопок ниже:",
            reply_markup=work_type_kb(),
        )
        return

    work_type_key = WORK_TYPES[text]  # "kursovaya", "referat", ...
    await state.update_data(work_type=work_type_key, work_type_display=text)

    # Конспект — особый флоу
    if work_type_key == "konspekt":
        await message.answer(
            f"✅ Тип работы: <b>{text}</b>\n\n"
            "Шаг 1 из 5 — введи <b>название</b> конспекта:",
            reply_markup=nav_kb(),
            parse_mode="HTML",
        )
        await state.set_state(KonspektStates.title)
        return

    await message.answer(
        f"✅ Тип работы: <b>{text}</b>\n\n"
        "Шаг 2 из 7 — введи <b>тему</b> работы:",
        reply_markup=nav_kb(),
        parse_mode="HTML",
    )
    await state.set_state(OrderStates.topic)


# ─── Шаг 2: тема ─────────────────────────────────────────────────────────────

@router.message(OrderStates.topic, F.text)
async def step_topic(message: Message, state: FSMContext) -> None:
    text = message.text.strip()
    if text == "◀️ Назад":
        await message.answer(
            "Шаг 1 из 7 — выбери <b>тип работы</b>:",
            reply_markup=work_type_kb(),
            parse_mode="HTML",
        )
        await state.set_state(OrderStates.work_type)
        return

    if len(text) < 3:
        await message.answer("Тема слишком короткая. Введи подробнее:", reply_markup=nav_kb())
        return

    await state.update_data(topic=text)
    await message.answer(
        f"✅ Тема: <b>{text}</b>\n\n"
        "Шаг 3 из 7 — введи <b>предмет</b> (дисциплину):",
        reply_markup=nav_kb(),
        parse_mode="HTML",
    )
    await state.set_state(OrderStates.subject)


@router.message(OrderStates.topic)
async def step_topic_fallback(message: Message) -> None:
    await message.answer("✏️ Введи тему работы текстом:", reply_markup=nav_kb())


# ─── Шаг 3: предмет ──────────────────────────────────────────────────────────

@router.message(OrderStates.subject, F.text)
async def step_subject(message: Message, state: FSMContext) -> None:
    text = message.text.strip()
    if text == "◀️ Назад":
        data = await state.get_data()
        hint = f"\n\nТекущая тема: <i>{data.get('topic', '')}</i>" if data.get("topic") else ""
        await message.answer(
            f"Шаг 2 из 7 — введи <b>тему</b> работы:{hint}",
            reply_markup=nav_kb(),
            parse_mode="HTML",
        )
        await state.set_state(OrderStates.topic)
        return

    await state.update_data(subject=text)
    data = await state.get_data()
    unit = "слайдов" if data.get("work_type") == "prezentaciya" else "страниц"
    await message.answer(
        f"✅ Предмет: <b>{text}</b>\n\n"
        f"Шаг 4 из 7 — введи <b>объём</b> работы в {unit} (только цифры):",
        reply_markup=nav_kb(),
        parse_mode="HTML",
    )
    await state.set_state(OrderStates.pages)


@router.message(OrderStates.subject)
async def step_subject_fallback(message: Message) -> None:
    await message.answer("✏️ Введи предмет (дисциплину) текстом:", reply_markup=nav_kb())


# ─── Шаг 4: объём ────────────────────────────────────────────────────────────

@router.message(OrderStates.pages, F.text)
async def step_pages(message: Message, state: FSMContext) -> None:
    text = (message.text or "").strip()
    if text == "◀️ Назад":
        data = await state.get_data()
        hint = f"\n\nТекущий предмет: <i>{data.get('subject', '')}</i>" if data.get("subject") else ""
        await message.answer(
            f"Шаг 3 из 7 — введи <b>предмет</b> (дисциплину):{hint}",
            reply_markup=nav_kb(),
            parse_mode="HTML",
        )
        await state.set_state(OrderStates.subject)
        return

    data = await state.get_data()
    unit = "слайдов" if data.get("work_type") == "prezentaciya" else "страниц"
    unit_short = "сл." if data.get("work_type") == "prezentaciya" else "стр."

    if not text.isdigit() or int(text) < 1 or int(text) > 500:
        await message.answer(f"Введи корректное число {unit} (от 1 до 500):", reply_markup=nav_kb())
        return

    pages = int(text)
    await state.update_data(pages=pages)
    await message.answer(
        f"✅ Объём: <b>{pages} {unit_short}</b>\n\n"
        "Шаг 5 из 7 — укажи <b>дедлайн</b> (например: «15 января», «через 3 дня», «01.02.2026»):",
        reply_markup=nav_kb(),
        parse_mode="HTML",
    )
    await state.set_state(OrderStates.deadline)


@router.message(OrderStates.pages)
async def step_pages_fallback(message: Message, state: FSMContext) -> None:
    data = await state.get_data()
    unit = "слайдов" if data.get("work_type") == "prezentaciya" else "страниц"
    await message.answer(f"✏️ Введи количество {unit} числом (например: 10):", reply_markup=nav_kb())


# ─── Шаг 5: дедлайн ──────────────────────────────────────────────────────────

@router.message(OrderStates.deadline, F.text)
async def step_deadline(message: Message, state: FSMContext) -> None:
    text = message.text.strip()
    if text == "◀️ Назад":
        data = await state.get_data()
        unit = "слайдов" if data.get("work_type") == "prezentaciya" else "страниц"
        unit_short = "сл." if data.get("work_type") == "prezentaciya" else "стр."
        hint = f"\n\nТекущий объём: <i>{data.get('pages', '')} {unit_short}</i>" if data.get("pages") else ""
        await message.answer(
            f"Шаг 4 из 7 — введи <b>объём</b> работы в {unit}:{hint}",
            reply_markup=nav_kb(),
            parse_mode="HTML",
        )
        await state.set_state(OrderStates.pages)
        return

    await state.update_data(deadline=text)
    await message.answer(
        f"✅ Дедлайн: <b>{text}</b>\n\n"
        "Шаг 6 из 7 — напиши <b>комментарий</b> к заявке: доп. требования, пожелания, "
        "что важно учесть.\n\n"
        "Если нечего добавить — нажми <b>«Пропустить»</b>.",
        reply_markup=skip_nav_kb(),
        parse_mode="HTML",
    )
    await state.set_state(OrderStates.comment)


@router.message(OrderStates.deadline)
async def step_deadline_fallback(message: Message) -> None:
    await message.answer(
        "✏️ Укажи дедлайн текстом (например: «15 мая», «через 3 дня», «01.06.2025»):",
        reply_markup=nav_kb(),
    )


# ─── Шаг 6: комментарий (опционально) ────────────────────────────────────────

@router.message(OrderStates.comment, F.text)
async def step_comment(message: Message, state: FSMContext) -> None:
    text = message.text.strip()
    if text == "◀️ Назад":
        data = await state.get_data()
        hint = f"\n\nТекущий дедлайн: <i>{data.get('deadline', '')}</i>" if data.get("deadline") else ""
        await message.answer(
            f"Шаг 5 из 7 — укажи <b>дедлайн</b>:{hint}",
            reply_markup=nav_kb(),
            parse_mode="HTML",
        )
        await state.set_state(OrderStates.deadline)
        return

    comment = None if text == "Пропустить" else text
    await state.update_data(comment=comment)
    await message.answer(
        "Шаг 7 из 7 — загрузи <b>шаблон и любые файлы для выполнения этой работы</b>.\n\n"
        "Подойдёт всё: методичка, требования преподавателя, текст, фото, .docx, .pdf, .pptx и т.д.\n"
        "Если нечего прикреплять — нажми <b>«Пропустить»</b>.",
        reply_markup=skip_nav_kb(),
        parse_mode="HTML",
    )
    await state.set_state(OrderStates.file_upload)


@router.message(OrderStates.comment)
async def step_comment_fallback(message: Message) -> None:
    await message.answer(
        "✏️ Напиши комментарий текстом или нажми кнопку:",
        reply_markup=skip_nav_kb(),
    )


# ─── Шаг 7: файл (опционально) ───────────────────────────────────────────────

@router.message(OrderStates.file_upload, F.text == "Пропустить")
async def step_file_skip(message: Message, state: FSMContext, bot: Bot) -> None:
    """Студент пропустил загрузку файла"""
    await state.update_data(student_file_id=None, student_filename=None)
    await _show_confirmation(message, state, bot)


@router.message(OrderStates.file_upload, F.text == "◀️ Назад")
async def step_file_back(message: Message, state: FSMContext) -> None:
    """Назад из шага файла → к комментарию"""
    data = await state.get_data()
    comment = data.get("comment")
    hint = f"\n\nТекущий комментарий: <i>{comment}</i>" if comment else ""
    await message.answer(
        f"Шаг 6 из 7 — напиши <b>комментарий</b> к заявке:{hint}\n\n"
        "Если нечего добавить — нажми <b>«Пропустить»</b>.",
        reply_markup=skip_nav_kb(),
        parse_mode="HTML",
    )
    await state.set_state(OrderStates.comment)


@router.message(OrderStates.file_upload, F.document)
async def step_file_upload(message: Message, state: FSMContext, bot: Bot) -> None:
    """Студент загрузил файл (любой формат)"""
    doc: Document = message.document
    filename = doc.file_name or "файл"
    await state.update_data(student_file_id=doc.file_id, student_filename=filename)
    await message.answer(f"✅ Файл <b>{filename}</b> принят.", parse_mode="HTML")
    await _show_confirmation(message, state, bot)


@router.message(OrderStates.file_upload, F.photo)
async def step_file_photo(message: Message, state: FSMContext, bot: Bot) -> None:
    """Студент прислал фото как образец оформления"""
    photo = message.photo[-1]  # самое большое разрешение
    filename = f"photo_sample_{photo.file_id[:10]}.jpg"
    await state.update_data(student_file_id=photo.file_id, student_filename=filename)
    await message.answer("✅ Фото принято как образец оформления.", parse_mode="HTML")
    await _show_confirmation(message, state, bot)


@router.message(OrderStates.file_upload)
async def step_file_wrong(message: Message) -> None:
    await message.answer(
        "Отправь файл (любой формат) или фото. Или нажми «Пропустить».",
        reply_markup=skip_nav_kb(),
    )


async def _show_confirmation(message: Message, state: FSMContext, bot: Bot) -> None:
    """Показывает итоговую карточку заявки и кнопки подтверждения"""
    data = await state.get_data()

    work_display = data.get("work_type_display", data.get("work_type", "—"))
    comment_text = data.get("comment") or "—"
    file_info = f"📎 {data.get('student_filename')}" if data.get("student_filename") else "не загружен"

    # Рассчитываем стартовую цену (единый прайс из services/pricing.py)
    pages = data.get("pages", 0)
    work_type = data.get("work_type", "")
    deadline_str = data.get("deadline", "")
    from services.pricing import calculate_starting_price
    price, unit_label, price_info = calculate_starting_price(work_type, pages, deadline_str)

    # Расшифровка цены — базовая ставка + коэффициент срочности
    base_p = price_info["base_price"]
    urg_mul = price_info["urgency_multiplier"]
    urg_lbl = price_info["urgency_label"]
    if urg_mul > 1.0:
        price_breakdown = (
            f"💰 <b>Предварительная стоимость:</b> {price} руб.\n"
            f"   Базовая ставка: {base_p} ₽\n"
            f"   Коэффициент срочности: × {urg_mul} ({urg_lbl})\n"
        )
    else:
        price_breakdown = (
            f"💰 <b>Предварительная стоимость:</b> {price} руб. "
            f"<i>({urg_lbl})</i>\n"
        )

    card = (
        f"📋 <b>Ваша заявка</b>\n\n"
        f"📚 <b>Тип:</b> {work_display}\n"
        f"📝 <b>Тема:</b> {data.get('topic', '—')}\n"
        f"🎓 <b>Предмет:</b> {data.get('subject', '—')}\n"
        f"📏 <b>Объём:</b> {unit_label}\n"
        f"📅 <b>Дедлайн:</b> {data.get('deadline', '—')}\n"
        f"💬 <b>Комментарий:</b> {comment_text}\n"
        f"📎 <b>Шаблон:</b> {file_info}\n\n"
        f"{price_breakdown}"
        f"<i>Финальная цена согласуется администратором с учётом сложности темы и требований к источникам.</i>\n\n"
        f"Всё верно? Нажми <b>«Подтвердить»</b> для отправки заявки."
    )

    # Создаём временный заказ в БД для получения ID
    async with AsyncSessionLocal() as session:
        from db.crud import get_user_by_tg_id
        user = await get_user_by_tg_id(session, message.from_user.id)

        order = await create_order(
            session=session,
            user_id=user.id,
            work_type=data.get("work_type"),
            topic=data.get("topic"),
            subject=data.get("subject"),
            pages=pages,
            deadline=data.get("deadline"),
            comment=data.get("comment"),
            price=price,
        )
        order_id = order.id

    await state.update_data(order_id=order_id, price=price)
    await state.set_state(OrderStates.confirm)

    from bot.keyboards.student_kb import confirm_kb
    await message.answer(card, reply_markup=confirm_kb(order_id), parse_mode="HTML")


# ═══════════════════════════════════════════════════════════════════════════════
# ─── КОНСПЕКТ: особый флоу ────────────────────────────────────────────────────
# ═══════════════════════════════════════════════════════════════════════════════

# ── Шаг К1: Название ──────────────────────────────────────────────────────────

@router.message(KonspektStates.title, F.text)
async def kstep_title(message: Message, state: FSMContext) -> None:
    text = message.text.strip()
    if text == "◀️ Назад":
        await message.answer("Шаг 1 из 7 — выбери <b>тип работы</b>:",
                             reply_markup=work_type_kb(), parse_mode="HTML")
        await state.set_state(OrderStates.work_type)
        return
    if len(text) < 2:
        await message.answer("Название слишком короткое. Введи подробнее:", reply_markup=nav_kb())
        return
    await state.update_data(k_title=text)
    await message.answer(
        f"✅ Название: <b>{text}</b>\n\n"
        "Шаг 2 из 6 — введи <b>дисциплину</b>:",
        reply_markup=nav_kb(), parse_mode="HTML",
    )
    await state.set_state(KonspektStates.discipline)


@router.message(KonspektStates.title)
async def kstep_title_fallback(message: Message) -> None:
    await message.answer("✏️ Введи название конспекта текстом:", reply_markup=nav_kb())


# ── Шаг К2: Дисциплина ────────────────────────────────────────────────────────

@router.message(KonspektStates.discipline, F.text)
async def kstep_discipline(message: Message, state: FSMContext) -> None:
    text = message.text.strip()
    if text == "◀️ Назад":
        data = await state.get_data()
        hint = f"\n\nТекущее название: <i>{data.get('k_title', '')}</i>" if data.get("k_title") else ""
        await message.answer(f"Шаг 1 из 6 — введи <b>название</b> конспекта:{hint}",
                             reply_markup=nav_kb(), parse_mode="HTML")
        await state.set_state(KonspektStates.title)
        return
    await state.update_data(k_discipline=text)
    await message.answer(
        f"✅ Дисциплина: <b>{text}</b>\n\n"
        "Шаг 3 из 6 — укажи <b>дедлайн</b> (например: «15 января», «через 3 дня»):",
        reply_markup=nav_kb(), parse_mode="HTML",
    )
    await state.set_state(KonspektStates.deadline)


@router.message(KonspektStates.discipline)
async def kstep_discipline_fallback(message: Message) -> None:
    await message.answer("✏️ Введи дисциплину текстом:", reply_markup=nav_kb())


# ── Шаг К3: Дедлайн ───────────────────────────────────────────────────────────

@router.message(KonspektStates.deadline, F.text)
async def kstep_deadline(message: Message, state: FSMContext) -> None:
    text = message.text.strip()
    if text == "◀️ Назад":
        data = await state.get_data()
        hint = f"\n\nТекущая дисциплина: <i>{data.get('k_discipline', '')}</i>" if data.get("k_discipline") else ""
        await message.answer(f"Шаг 2 из 6 — введи <b>дисциплину</b>:{hint}",
                             reply_markup=nav_kb(), parse_mode="HTML")
        await state.set_state(KonspektStates.discipline)
        return
    await state.update_data(k_deadline=text)
    await message.answer(
        f"✅ Дедлайн: <b>{text}</b>\n\n"
        "Шаг 4 из 6 — выбери, <b>что конспектировать</b>:",
        reply_markup=konspekt_source_kb(), parse_mode="HTML",
    )
    await state.set_state(KonspektStates.source_type)


@router.message(KonspektStates.deadline)
async def kstep_deadline_fallback(message: Message) -> None:
    await message.answer("✏️ Введи дедлайн текстом:", reply_markup=nav_kb())


# ── Шаг К4: Тип источника + ссылка ───────────────────────────────────────────

@router.message(KonspektStates.source_type, F.text)
async def kstep_source_type(message: Message, state: FSMContext) -> None:
    text = message.text.strip()
    if text == "◀️ Назад":
        data = await state.get_data()
        hint = f"\n\nТекущий дедлайн: <i>{data.get('k_deadline', '')}</i>" if data.get("k_deadline") else ""
        await message.answer(f"Шаг 3 из 6 — укажи <b>дедлайн</b>:{hint}",
                             reply_markup=nav_kb(), parse_mode="HTML")
        await state.set_state(KonspektStates.deadline)
        return
    if text not in KONSPEKT_SOURCE_TYPES:
        await message.answer("Выбери тип из кнопок ниже:", reply_markup=konspekt_source_kb())
        return
    await state.update_data(k_source_type=text)
    await message.answer(
        f"✅ Что конспектировать: <b>{text}</b>\n\n"
        "Прикрепи источник:\n"
        "• <b>Видео или аудио</b> → пришли <b>ссылку</b>\n"
        "• <b>Фото или документ</b> → прикрепи <b>файл</b> (один) или <b>zip-архив</b> (если несколько)\n"
        "• Ссылка на облачное хранилище (Google Drive, Яндекс.Диск и т.п.) тоже подойдёт\n\n"
        "Если нет — нажми <b>«Пропустить»</b>.",
        reply_markup=skip_nav_kb(), parse_mode="HTML",
    )
    await state.set_state(KonspektStates.source_link)


@router.message(KonspektStates.source_type)
async def kstep_source_type_fallback(message: Message) -> None:
    await message.answer("Выбери тип из кнопок:", reply_markup=konspekt_source_kb())


def _source_link_prompt() -> str:
    return (
        "Прикрепи источник:\n"
        "• <b>Видео или аудио</b> → пришли <b>ссылку</b>\n"
        "• <b>Фото или документ</b> → прикрепи <b>файл</b> (один) или <b>zip-архив</b> (если несколько)\n"
        "• Ссылка на облачное хранилище тоже подойдёт\n\n"
        "Если нет — нажми «Пропустить»."
    )


async def _kstep_source_link_done(message: Message, state: FSMContext) -> None:
    """Переходим к шагу материалов после получения источника"""
    await message.answer(
        "Шаг 5 из 6 — загрузи <b>доп. материалы</b> (методичка, учебник, задание и т.п.).\n\n"
        "• Один файл — прикрепи напрямую (любой формат)\n"
        "• Несколько — упакуй в <b>.zip</b>\n"
        "• Или пришли <b>ссылку</b> на хранилище (Google Drive, Яндекс.Диск и т.п.)\n\n"
        "Если материалов нет — нажми <b>«Пропустить»</b>.",
        reply_markup=konspekt_materials_kb(), parse_mode="HTML",
    )
    await state.set_state(KonspektStates.materials)


@router.message(KonspektStates.source_link, F.text)
async def kstep_source_link(message: Message, state: FSMContext) -> None:
    text = message.text.strip()
    if text == "◀️ Назад":
        await message.answer(
            "Шаг 4 из 6 — выбери, <b>что конспектировать</b>:",
            reply_markup=konspekt_source_kb(), parse_mode="HTML",
        )
        await state.set_state(KonspektStates.source_type)
        return
    value = None if text == "Пропустить" else text
    await state.update_data(k_source_link=value, k_source_file_id=None, k_source_filename=None)
    await _kstep_source_link_done(message, state)


@router.message(KonspektStates.source_link, F.document)
async def kstep_source_link_doc(message: Message, state: FSMContext) -> None:
    """Студент прислал файл как источник (фото/документ)"""
    doc = message.document
    await state.update_data(
        k_source_link=None,
        k_source_file_id=doc.file_id,
        k_source_filename=doc.file_name or "файл",
    )
    await message.answer(f"✅ Файл <b>{doc.file_name}</b> принят как источник.", parse_mode="HTML")
    await _kstep_source_link_done(message, state)


@router.message(KonspektStates.source_link, F.photo)
async def kstep_source_link_photo(message: Message, state: FSMContext) -> None:
    """Студент прислал фото как источник"""
    photo = message.photo[-1]
    fname = f"source_photo_{photo.file_id[:10]}.jpg"
    await state.update_data(k_source_link=None, k_source_file_id=photo.file_id, k_source_filename=fname)
    await message.answer("✅ Фото принято как источник.", parse_mode="HTML")
    await _kstep_source_link_done(message, state)


@router.message(KonspektStates.source_link)
async def kstep_source_link_fallback(message: Message) -> None:
    await message.answer(
        _source_link_prompt(),
        reply_markup=skip_nav_kb(), parse_mode="HTML",
    )


# ── Шаг К5: Доп. материалы (файл / ссылка / пропустить) ─────────────────────

@router.message(KonspektStates.materials, F.text)
async def kstep_materials_text(message: Message, state: FSMContext) -> None:
    text = message.text.strip()
    if text == "◀️ Назад":
        data = await state.get_data()
        src = data.get("k_source_type", "")
        await message.answer(
            f"Отправь <b>ссылку на видео или аудио</b> для конспектирования.\n\n"
            f"Если ссылки нет — нажми «Пропустить».",
            reply_markup=skip_nav_kb(), parse_mode="HTML",
        )
        await state.set_state(KonspektStates.source_link)
        return
    material = None if text == "Пропустить" else text
    await state.update_data(k_material=material, k_material_file_id=None, k_material_filename=None)
    await _kstep_go_to_preferences(message, state)


@router.message(KonspektStates.materials, F.document)
async def kstep_materials_file(message: Message, state: FSMContext) -> None:
    """Студент прислал файл как доп. материал"""
    doc = message.document
    await state.update_data(
        k_material=None,
        k_material_file_id=doc.file_id,
        k_material_filename=doc.file_name or "файл",
    )
    await message.answer(f"✅ Файл <b>{doc.file_name}</b> принят.", parse_mode="HTML")
    await _kstep_go_to_preferences(message, state)


@router.message(KonspektStates.materials, F.photo)
async def kstep_materials_photo(message: Message, state: FSMContext) -> None:
    """Студент прислал фото как доп. материал"""
    photo = message.photo[-1]
    fname = f"material_photo_{photo.file_id[:10]}.jpg"
    await state.update_data(k_material=None, k_material_file_id=photo.file_id, k_material_filename=fname)
    await message.answer("✅ Фото принято как материал.", parse_mode="HTML")
    await _kstep_go_to_preferences(message, state)


@router.message(KonspektStates.materials)
async def kstep_materials_fallback(message: Message) -> None:
    await message.answer(
        "Отправь файл, фото, ссылку или нажми «Пропустить»:",
        reply_markup=konspekt_materials_kb(),
    )


async def _kstep_go_to_preferences(message: Message, state: FSMContext) -> None:
    await message.answer(
        "Шаг 6 из 6 — напиши <b>свои предпочтения</b> в свободной форме.\n\n"
        "Например: стиль изложения, объём, особые пожелания.\n"
        "Если нечего добавить — нажми <b>«Пропустить»</b>.",
        reply_markup=skip_nav_kb(), parse_mode="HTML",
    )
    await state.set_state(KonspektStates.preferences)


# ── Шаг К6: Предпочтения ──────────────────────────────────────────────────────

@router.message(KonspektStates.preferences, F.text)
async def kstep_preferences(message: Message, state: FSMContext, bot: Bot) -> None:
    text = message.text.strip()
    if text == "◀️ Назад":
        await message.answer(
            "Шаг 5 из 6 — загрузи <b>доп. материалы</b> или нажми «Пропустить»:",
            reply_markup=konspekt_materials_kb(), parse_mode="HTML",
        )
        await state.set_state(KonspektStates.materials)
        return
    prefs = None if text == "Пропустить" else text
    await state.update_data(k_preferences=prefs)
    await _show_konspekt_confirmation(message, state, bot)


@router.message(KonspektStates.preferences)
async def kstep_preferences_fallback(message: Message) -> None:
    await message.answer("✏️ Напиши предпочтения текстом или нажми «Пропустить»:", reply_markup=skip_nav_kb())


# ── Карточка подтверждения конспекта ──────────────────────────────────────────

async def _show_konspekt_confirmation(message: Message, state: FSMContext, bot: Bot) -> None:
    """Карточка подтверждения для конспекта"""
    data = await state.get_data()
    work_display = data.get("work_type_display", "Конспект")

    source_type = data.get("k_source_type") or "—"
    # Источник: ссылка или файл
    if data.get("k_source_link"):
        source_display = f"🔗 {data['k_source_link']}"
    elif data.get("k_source_filename"):
        source_display = f"📎 {data['k_source_filename']}"
    else:
        source_display = "—"
    # Материалы: ссылка или файл
    if data.get("k_material"):
        material_display = f"🔗 {data['k_material']}"
    elif data.get("k_material_filename"):
        material_display = f"📎 {data['k_material_filename']}"
    else:
        material_display = "—"
    prefs_text = data.get("k_preferences") or "—"

    # Конспект: стартовая цена 200 ₽ + коэффициент срочности по дедлайну
    from services.pricing import calculate_starting_price
    price, _, _ = calculate_starting_price("konspekt", 10, data.get("k_deadline", ""))

    card = (
        f"📋 <b>Ваша заявка</b>\n\n"
        f"📚 <b>Тип:</b> {work_display}\n"
        f"📝 <b>Название:</b> {data.get('k_title', '—')}\n"
        f"🎓 <b>Дисциплина:</b> {data.get('k_discipline', '—')}\n"
        f"📅 <b>Дедлайн:</b> {data.get('k_deadline', '—')}\n"
        f"🎬 <b>Что конспектировать:</b> {source_type}\n"
        f"📂 <b>Источник:</b> {source_display}\n"
        f"📎 <b>Доп. материалы:</b> {material_display}\n"
        f"💬 <b>Предпочтения:</b> {prefs_text}\n\n"
        f"💰 <b>Предварительная стоимость:</b> {price} руб.\n"
        f"<i>Итоговая цена согласовывается администратором — может отличаться от стартовой в зависимости от сложности темы и срочности.</i>\n\n"
        f"Всё верно? Нажми <b>«Подтвердить»</b> для отправки заявки."
    )

    # Комментарий для БД
    comment_parts = []
    if data.get("k_source_type"):
        comment_parts.append(f"Тип: {data['k_source_type']}")
    if data.get("k_source_link"):
        comment_parts.append(f"Источник (ссылка): {data['k_source_link']}")
    if data.get("k_source_filename"):
        comment_parts.append(f"Источник (файл): {data['k_source_filename']}")
    if data.get("k_material"):
        comment_parts.append(f"Материалы (ссылка): {data['k_material']}")
    if data.get("k_material_filename"):
        comment_parts.append(f"Материалы (файл): {data['k_material_filename']}")
    if data.get("k_preferences"):
        comment_parts.append(f"Предпочтения: {data['k_preferences']}")
    combined_comment = "\n".join(comment_parts) or None

    async with AsyncSessionLocal() as session:
        from db.crud import get_user_by_tg_id
        db_user = await get_user_by_tg_id(session, message.from_user.id)
        order = await create_order(
            session=session,
            user_id=db_user.id,
            work_type="konspekt",
            topic=data.get("k_title"),
            subject=data.get("k_discipline"),
            pages=1,
            deadline=data.get("k_deadline"),
            comment=combined_comment,
            price=price,
        )
        order_id = order.id

    # Приоритет файла для отправки админу: источник > материал
    file_id_for_admin = data.get("k_source_file_id") or data.get("k_material_file_id")
    filename_for_admin = data.get("k_source_filename") or data.get("k_material_filename")
    await state.update_data(
        order_id=order_id,
        price=price,
        student_file_id=file_id_for_admin,
        student_filename=filename_for_admin,
    )
    await state.set_state(KonspektStates.confirm)

    from bot.keyboards.student_kb import confirm_kb
    await message.answer(card, reply_markup=confirm_kb(order_id), parse_mode="HTML")


# ─── Подтверждение заявки ─────────────────────────────────────────────────────

@router.callback_query(F.data.startswith("confirm_order_"))
async def cb_confirm_order(callback: CallbackQuery, state: FSMContext, bot: Bot, user: User) -> None:
    order_id = int(callback.data.split("_")[-1])
    data = await state.get_data()

    async with AsyncSessionLocal() as session:
        order = await get_order(session, order_id)
        if order is None:
            await callback.answer("Заявка не найдена.", show_alert=True)
            return

        # Строим промпт
        has_file = bool(data.get("student_file_id"))
        prompt = build_prompt(order, has_student_file=has_file)

        # Сохраняем промпт в заказе
        from db.crud import update_order_prompt
        await update_order_prompt(session, order_id, prompt)
        await update_order_status(session, order_id, OrderStatus.PENDING)

        # Сохраняем файл студента если есть
        if has_file:
            student_file_id = data.get("student_file_id")
            student_filename = data.get("student_filename", "template")

            # Скачиваем файл на сервер
            uploads_dir = settings.UPLOADS_DIR
            os.makedirs(uploads_dir, exist_ok=True)
            local_path = os.path.join(uploads_dir, f"order_{order_id}_student_{student_filename}")

            file_info = await bot.get_file(student_file_id)
            await bot.download_file(file_info.file_path, local_path)

            await save_file(
                session=session,
                order_id=order_id,
                file_type=FileType.STUDENT_TEMPLATE,
                tg_file_id=student_file_id,
                filename=student_filename,
                local_path=local_path,
            )

        # Выбираем серверный шаблон для админа (без учёта файла студента —
        # файл студента отправляем отдельно через tg_file_id)
        template_path = get_template_path(order.work_type or "drugoe", None)

    await state.clear()

    # Уведомляем студента
    await callback.message.edit_text(
        f"✅ <b>Заявка #{order_id} отправлена!</b>\n\n"
        f"Я уведомлю тебя, когда администратор примет решение.\n"
        f"Отслеживай статус через /my_orders",
        parse_mode="HTML",
    )
    await callback.message.answer("Главное меню:", reply_markup=main_menu_kb(user.role))

    # Уведомляем администратора
    student_tg_file_id = data.get("student_file_id") if has_file else None
    student_filename = data.get("student_filename") if has_file else None
    await _notify_admin(bot, order_id, order, prompt, template_path, has_file,
                        student_tg_file_id, student_filename)
    await callback.answer()


@router.callback_query(F.data.startswith("cancel_order_"))
async def cb_cancel_order(callback: CallbackQuery, state: FSMContext, user: User) -> None:
    order_id = int(callback.data.split("_")[-1])

    async with AsyncSessionLocal() as session:
        await update_order_status(session, order_id, OrderStatus.CANCELLED)

    await state.clear()
    await callback.message.edit_text("❌ Заявка отменена.")
    await callback.message.answer("Главное меню:", reply_markup=main_menu_kb(user.role))
    await callback.answer()


@router.callback_query(F.data.startswith("edit_order_"))
async def cb_edit_order(callback: CallbackQuery, state: FSMContext) -> None:
    """Сброс заявки для повторного заполнения"""
    await callback.answer("Начинаем заново...")
    await callback.message.answer(
        "📋 Начнём заново. Выбери тип работы:",
        reply_markup=work_type_kb(),
    )
    await state.set_state(OrderStates.work_type)


async def _notify_admin(
    bot: Bot,
    order_id: int,
    order,
    prompt: str,
    template_path: str | None,
    has_student_file: bool,
    student_tg_file_id: str | None = None,
    student_filename: str | None = None,
) -> None:
    """Отправляет карточку заявки администратору"""
    work_display = WORK_TYPE_NAMES.get(order.work_type or "drugoe", order.work_type or "—")
    comment_text = order.comment or "—"
    file_info = "✅ загружен студентом" if has_student_file else "наш шаблон"

    card = (
        f"🔔 <b>Новая заявка #{order_id}</b>\n\n"
        f"👤 Студент: @{order.user.username or '—'} ({order.user.full_name or '—'})\n\n"
        f"📚 <b>Тип:</b> {work_display}\n"
        f"📝 <b>Тема:</b> {order.topic}\n"
        f"🎓 <b>Предмет:</b> {order.subject}\n"
        f"📏 <b>Объём:</b> {order.pages} стр.\n"
        f"📅 <b>Дедлайн:</b> {order.deadline}\n"
        f"💬 <b>Комментарий:</b> {comment_text}\n"
        f"📎 <b>Шаблон:</b> {file_info}\n\n"
        f"💰 <b>Цена:</b> {order.price} руб."
    )

    try:
        await bot.send_message(
            chat_id=settings.ADMIN_TG_ID,
            text=card,
            reply_markup=admin_order_kb(order_id),
            parse_mode="HTML",
        )

        # Отправляем файл студента напрямую через tg_file_id
        if has_student_file and student_tg_file_id:
            fname = (student_filename or "").lower()
            if fname.endswith((".jpg", ".jpeg", ".png", ".webp", ".gif")):
                await bot.send_photo(
                    chat_id=settings.ADMIN_TG_ID,
                    photo=student_tg_file_id,
                    caption=f"📸 Файл студента — заказ #{order_id}",
                )
            else:
                await bot.send_document(
                    chat_id=settings.ADMIN_TG_ID,
                    document=student_tg_file_id,
                    caption=f"📎 Файл студента — заказ #{order_id}",
                )

        # Отправляем шаблон файлом если найден
        if template_path and os.path.isfile(template_path):
            with open(template_path, "rb") as f:
                await bot.send_document(
                    chat_id=settings.ADMIN_TG_ID,
                    document=f,
                    caption=f"📄 Шаблон для заказа #{order_id}",
                )
    except Exception as e:
        logger.error(f"Ошибка уведомления администратора: {e}")


# ─── Подтверждение / отмена цены студентом ────────────────────────────────────

@router.callback_query(F.data.startswith("student_price_ok_"))
async def cb_student_price_ok(callback: CallbackQuery, user, bot: Bot) -> None:
    order_id = int(callback.data.split("_")[-1])

    # Студент согласился с ценой → ТОЛЬКО теперь заявка реально уходит в работу.
    work_type = ""
    async with AsyncSessionLocal() as session:
        order = await get_order(session, order_id)
        if order is not None:
            work_type = order.work_type or ""
            await update_order_status(session, order_id, OrderStatus.IN_WORK)

    await callback.message.edit_text(
        callback.message.text + "\n\n✅ <b>Вы подтвердили цену. Работа начата!</b>",
        parse_mode="HTML",
    )
    await callback.answer("Отлично!")
    # Уведомляем админа — теперь можно отправлять редактору
    try:
        from config import settings as _s
        from bot.keyboards.admin_kb import admin_after_accept_kb
        await bot.send_message(
            chat_id=_s.ADMIN_TG_ID,
            text=(
                f"✅ <b>Студент подтвердил цену по заказу #{order_id}.</b>\n\n"
                f"Заявка в работе — можно отправлять редактору."
            ),
            reply_markup=admin_after_accept_kb(order_id, work_type),
            parse_mode="HTML",
        )
    except Exception:
        pass


@router.callback_query(F.data.startswith("student_price_cancel_"))
async def cb_student_price_cancel(callback: CallbackQuery, user, bot: Bot) -> None:
    order_id = int(callback.data.split("_")[-1])
    async with AsyncSessionLocal() as session:
        await update_order_status(session, order_id, OrderStatus.CANCELLED)

    await callback.message.edit_text(
        callback.message.text + "\n\n❌ <b>Заявка отменена.</b>",
        parse_mode="HTML",
    )
    await callback.answer("Заявка отменена.")
    # Уведомляем админа
    try:
        from config import settings as _s
        await bot.send_message(
            chat_id=_s.ADMIN_TG_ID,
            text=f"❌ Студент отменил заявку #{order_id} (не согласен с ценой).",
        )
    except Exception:
        pass


# ─── Торг: студент предлагает свою цену ──────────────────────────────────────

@router.callback_query(F.data.startswith("student_price_offer_"))
async def cb_student_price_offer_start(cb: CallbackQuery, state: FSMContext, user) -> None:
    """Студент нажал «💬 Предложить свою цену» — спрашиваем сумму."""
    try:
        order_id = int(cb.data.removeprefix("student_price_offer_"))
    except ValueError:
        await cb.answer("Ошибка", show_alert=True)
        return

    async with AsyncSessionLocal() as session:
        order = await get_order(session, order_id)
        if order is None or order.user_id != user.id:
            await cb.answer("Заказ не найден", show_alert=True)
            return
        current_price = order.price or 0

    await state.set_state(PriceNegotiationStates.waiting_student_offer)
    await state.update_data(negotiate_order_id=order_id, current_price=current_price)

    await cb.answer()
    await cb.message.answer(
        f"💬 <b>Своя цена — заказ #{order_id}</b>\n\n"
        f"Текущая цена: <b>{current_price:.0f} ₽</b>\n\n"
        f"Напишите сумму, которую готовы заплатить (только число, например: <code>350</code>).\n"
        f"Администратор рассмотрит и примет решение.\n\n"
        f"Чтобы отменить — отправьте /cancel",
        parse_mode="HTML",
        reply_markup=remove_kb(),
    )


@router.message(PriceNegotiationStates.waiting_student_offer, F.text == "/cancel")
async def cb_student_price_offer_cancel(message: Message, state: FSMContext, user) -> None:
    await state.clear()
    await message.answer("Торг отменён.", reply_markup=main_menu_kb(user.role))


@router.message(PriceNegotiationStates.waiting_student_offer, F.text)
async def cb_student_price_offer_submit(message: Message, state: FSMContext, user, bot: Bot) -> None:
    """Студент прислал свою цену. Уведомляем админа с кнопками."""
    text = (message.text or "").strip().replace(" ", "").replace("₽", "").replace("руб", "")
    try:
        offered_price = int(float(text))
    except (ValueError, TypeError):
        await message.answer("Введите цену числом (например: <code>350</code>).", parse_mode="HTML")
        return

    if offered_price <= 0 or offered_price > 1_000_000:
        await message.answer("Цена должна быть от 1 до 1 000 000 ₽.")
        return

    data = await state.get_data()
    order_id = data.get("negotiate_order_id")
    current_price = data.get("current_price", 0)
    await state.clear()

    if not order_id:
        await message.answer("Ошибка состояния. Создайте заявку заново.", reply_markup=main_menu_kb(user.role))
        return

    # Уведомление студенту
    await message.answer(
        f"✅ Ваше предложение <b>{offered_price} ₽</b> по заказу #{order_id} отправлено администратору.\n\n"
        f"Ожидайте ответа в этом чате.",
        parse_mode="HTML",
        reply_markup=main_menu_kb(user.role),
    )

    # Уведомление админу
    from bot.keyboards.student_kb import admin_price_negotiation_kb
    student_label = f"@{user.username}" if user.username else (user.full_name or f"id{user.tg_id}")
    diff = offered_price - current_price
    diff_str = f"+{diff} ₽" if diff > 0 else f"{diff} ₽"

    try:
        await bot.send_message(
            chat_id=settings.ADMIN_TG_ID,
            text=(
                f"💬 <b>Торг по заказу #{order_id}</b>\n\n"
                f"Студент: {student_label} (tg_id {user.tg_id})\n"
                f"Текущая цена: <b>{current_price:.0f} ₽</b>\n"
                f"Предложение студента: <b>{offered_price} ₽</b> ({diff_str})"
            ),
            parse_mode="HTML",
            reply_markup=admin_price_negotiation_kb(order_id),
        )
        # Сохраняем offered_price в состоянии админа через простой in-memory dict
        # (используем audit-log как лёгкое хранилище)
        from web.audit import log_admin_action
        await log_admin_action(
            admin_tg_id=settings.ADMIN_TG_ID,
            action="student_price_offer",
            target_type="order",
            target_id=order_id,
            details={"current_price": current_price, "offered_price": offered_price},
        )
    except Exception as e:
        logger.error(f"Не удалось уведомить админа о торге #{order_id}: {e}")


# ─── /my_orders ───────────────────────────────────────────────────────────────

@router.message(Command("my_orders"))
@router.message(F.text == "📂 Мои заказы")
async def cmd_my_orders(message: Message, user: User) -> None:
    async with AsyncSessionLocal() as session:
        orders = await get_user_orders(session, user.id)

    if not orders:
        await message.answer("У тебя пока нет заявок. Создай первую через «Новая заявка»!")
        return

    # Сначала общее сообщение
    await message.answer(f"📂 <b>Твои заявки</b> ({len(orders)}):", parse_mode="HTML")

    for order in orders:
        work_display = WORK_TYPE_NAMES.get(order.work_type or "drugoe", order.work_type or "—")
        status_raw = order.status.value if hasattr(order.status, "value") else order.status
        topic = (order.topic or "")[:50]
        price = f"{order.price:.0f} ₽" if order.price else "—"
        revisions_used = order.revisions_used or 0

        # Если есть запрошенная правка (revision_comment не пуст) И заказ ещё в активной фазе —
        # показываем «🔄 На доработке у редактора» вместо обычного статуса.
        is_rework_pending = bool(order.revision_comment) and status_raw in (
            OrderStatus.PREVIEW_SENT.value,
            OrderStatus.AWAITING_PAYMENT.value,
            OrderStatus.EDITING.value,
        )
        if is_rework_pending:
            status_text = "🔄 На доработке у редактора"
        else:
            status_text = STATUS_DISPLAY.get(status_raw, status_raw)

        # Строка о правках: показываем для DONE и для пре-оплатных статусов
        revisions_line = ""
        show_revisions = status_raw == OrderStatus.DONE.value or status_raw in (
            OrderStatus.PREVIEW_SENT.value,
            OrderStatus.AWAITING_PAYMENT.value,
            OrderStatus.EDITING.value,
        )
        if show_revisions:
            remaining = max(0, MAX_REVISIONS - revisions_used)
            if remaining > 0:
                revisions_line = f"\n✏️ Бесплатных правок осталось: {remaining}/{MAX_REVISIONS}"
            else:
                revisions_line = f"\n✏️ Бесплатные правки исчерпаны ({revisions_used}/{MAX_REVISIONS})"

        card_text = (
            f"<b>#{order.id}</b> · {work_display} — {status_text}\n"
            f"📝 Тема: {topic}\n"
            f"💰 Цена: {price}"
            f"{revisions_line}"
        )

        # Подбираем кнопку:
        # - PENDING → отменить заявку
        # - доработка в работе → без кнопок (ждём редактора)
        # - PREVIEW_SENT / AWAITING_PAYMENT (без доработки) → превью + оплатить
        # - DONE с оставшимися правками → запросить правку
        # - остальное → без кнопок
        kb = None
        if status_raw == OrderStatus.PENDING.value:
            kb = cancel_order_kb(order.id)
        elif is_rework_pending:
            kb = None  # ждём редактора, кнопок нет
        elif status_raw in (OrderStatus.PREVIEW_SENT.value, OrderStatus.AWAITING_PAYMENT.value):
            from db.models import Payment
            from sqlalchemy import select
            async with AsyncSessionLocal() as s2:
                pr = await s2.execute(
                    select(Payment).where(Payment.order_id == order.id, Payment.status == "pending")
                    .order_by(Payment.created_at.desc()).limit(1)
                )
                pay = pr.scalar_one_or_none()
            short_url = f"{PAY_REDIRECT_BASE}/{pay.id}" if pay and pay.confirmation_url else None
            # Если правки исчерпаны — preview_actions_kb всё равно показывает «👀 Превью» + «💳 Оплатить»;
            # кнопки «На доработку» в этой клавиатуре нет вообще, так что менять не надо.
            # Но при просмотре превью callback покажет либо обе кнопки, либо только «Оплатить» — см. preview_payment_kb.
            kb = preview_actions_kb(order.id, short_url)
        elif status_raw == OrderStatus.DONE.value and revisions_used < MAX_REVISIONS:
            kb = revision_request_kb(order.id, revisions_used, MAX_REVISIONS)

        await message.answer(card_text, parse_mode="HTML", reply_markup=kb)


# ─── /cancel_order — отмена заявки студентом (только PENDING) ────────────────

@router.message(Command("cancel_order"))
async def cmd_cancel_order(message: Message, user: User) -> None:
    """Команда `/cancel_order <id>` — отменить свою заявку (только PENDING)."""
    args = (message.text or "").split(maxsplit=1)
    if len(args) < 2 or not args[1].strip().isdigit():
        await message.answer(
            "Использование: <code>/cancel_order &lt;номер_заявки&gt;</code>\n"
            "Пример: <code>/cancel_order 42</code>\n\n"
            "Список ваших заявок: /my_orders",
            parse_mode="HTML",
        )
        return

    order_id = int(args[1].strip())
    async with AsyncSessionLocal() as session:
        success, msg = await cancel_order_by_student(session, order_id, user.id)

    if success:
        await message.answer(
            f"✅ Заявка #{order_id} отменена.\n\nСоздать новую: /new_order",
        )
        # Уведомим админа
        try:
            from aiogram import Bot as BotCls
            bot: BotCls = message.bot
            await bot.send_message(
                chat_id=settings.ADMIN_TG_ID,
                text=f"❌ Студент @{user.username or user.tg_id} отменил заявку <b>#{order_id}</b>",
                parse_mode="HTML",
            )
        except Exception as e:
            logger.error(f"Не удалось уведомить админа об отмене: {e}")
    else:
        await message.answer(f"⚠️ {msg}")


@router.callback_query(F.data.startswith("stcancel_") & ~F.data.startswith("stcancel_yes_") & ~F.data.startswith("stcancel_no"))
async def cb_student_cancel_request(callback: CallbackQuery, user: User) -> None:
    """Студент нажал «❌ Отменить заявку» — показываем подтверждение."""
    order_id_str = callback.data.replace("stcancel_", "")
    if not order_id_str.isdigit():
        await callback.answer("Некорректные данные.", show_alert=True)
        return
    order_id = int(order_id_str)
    await callback.message.answer(
        f"❓ Точно отменить заявку <b>#{order_id}</b>?\n\n"
        f"Это действие необратимо. Если потом захотите — придётся создать новую заявку.",
        reply_markup=cancel_order_confirm_kb(order_id),
        parse_mode="HTML",
    )
    await callback.answer()


@router.callback_query(F.data.startswith("stcancel_yes_"))
async def cb_student_cancel_confirm(callback: CallbackQuery, user: User, bot: Bot) -> None:
    """Подтверждение отмены студентом."""
    order_id = int(callback.data.split("_")[-1])
    async with AsyncSessionLocal() as session:
        success, msg = await cancel_order_by_student(session, order_id, user.id)

    if success:
        await callback.message.edit_text(
            f"✅ Заявка <b>#{order_id}</b> отменена.\n\nСоздать новую: /new_order",
            parse_mode="HTML",
        )
        try:
            await bot.send_message(
                chat_id=settings.ADMIN_TG_ID,
                text=f"❌ Студент @{user.username or user.tg_id} отменил заявку <b>#{order_id}</b>",
                parse_mode="HTML",
            )
        except Exception as e:
            logger.error(f"Не удалось уведомить админа об отмене: {e}")
    else:
        await callback.message.edit_text(f"⚠️ {msg}")
    await callback.answer()


@router.callback_query(F.data == "stcancel_no")
async def cb_student_cancel_decline(callback: CallbackQuery) -> None:
    await callback.message.edit_text("✓ Отмена отменена — заявка осталась активной.")
    await callback.answer()


# ─── /refund — заявка на возврат ─────────────────────────────────────────────

@router.message(Command("refund"))
async def cmd_refund(message: Message, state: FSMContext, user: User) -> None:
    """Студент инициирует возврат за оплаченный заказ."""
    if user.role != "student":
        await message.answer("Команда доступна только студентам.")
        return

    async with AsyncSessionLocal() as session:
        paid_orders = await get_user_paid_orders(session, user.id)

    if not paid_orders:
        await message.answer(
            "У вас нет оплаченных заказов, по которым можно запросить возврат.\n\n"
            "Если вы только что отправили заявку и хотите её отменить (до оплаты) — "
            "используйте /my_orders и кнопку «Отменить заявку».",
        )
        return

    await state.set_state(RefundStates.choosing_order)
    await message.answer(
        "💸 <b>Заявка на возврат средств</b>\n\n"
        "Выберите заказ, по которому хотите оформить возврат.\n"
        f"Подробные правила: {DOC_URLS['refund']}",
        reply_markup=refund_order_select_kb(paid_orders),
        parse_mode="HTML",
    )


@router.callback_query(RefundStates.choosing_order, F.data.startswith("refund_pick_"))
async def cb_refund_pick(callback: CallbackQuery, state: FSMContext) -> None:
    order_id = int(callback.data.split("_")[-1])
    await state.update_data(refund_order_id=order_id)
    await state.set_state(RefundStates.waiting_reason)
    await callback.message.edit_text(
        f"📝 <b>Возврат по заказу #{order_id}</b>\n\n"
        f"Опишите причину возврата (минимум 10 символов).\n"
        f"Это поможет нам быстрее обработать заявку.\n\n"
        f"Отмена: /cancel",
        parse_mode="HTML",
    )
    await callback.answer()


@router.callback_query(F.data == "refund_cancel")
async def cb_refund_cancel(callback: CallbackQuery, state: FSMContext) -> None:
    await state.clear()
    await callback.message.edit_text("Запрос возврата отменён.")
    await callback.answer()


@router.message(RefundStates.waiting_reason, F.text)
async def refund_reason_received(message: Message, state: FSMContext, user: User, bot: Bot) -> None:
    text = (message.text or "").strip()
    if text.startswith("/cancel"):
        await state.clear()
        await message.answer("Запрос возврата отменён.")
        return
    if len(text) < 10:
        await message.answer("Причина слишком короткая. Опишите подробнее (мин. 10 символов).")
        return

    data = await state.get_data()
    order_id = data.get("refund_order_id")
    await state.clear()

    try:
        async with AsyncSessionLocal() as session:
            req = await create_refund_request(
                session, user_id=user.id, order_id=order_id, reason=text,
            )
    except ValueError as e:
        await message.answer(f"⚠️ {e}", reply_markup=main_menu_kb(user.role))
        return

    await message.answer(
        f"✅ <b>Заявка на возврат #{req.id} отправлена</b>\n\n"
        f"По заказу: <b>#{order_id}</b>\n"
        f"Срок рассмотрения: до <b>3 рабочих дней</b>.\n\n"
        f"Мы пришлём уведомление о решении.",
        reply_markup=main_menu_kb(user.role),
        parse_mode="HTML",
    )

    # Уведомляем админа
    try:
        from bot.keyboards.admin_kb import refund_action_kb
        name = f"@{user.username}" if user.username else (user.full_name or f"id{user.tg_id}")
        await bot.send_message(
            chat_id=settings.ADMIN_TG_ID,
            text=(
                f"📨 <b>Новая заявка на возврат #{req.id}</b>\n\n"
                f"👤 Студент: {name}\n"
                f"📦 Заказ: <b>#{order_id}</b>\n\n"
                f"<b>Причина:</b>\n<i>{text}</i>"
            ),
            reply_markup=refund_action_kb(req.id),
            parse_mode="HTML",
        )
    except Exception as e:
        logger.error(f"Не удалось уведомить админа о возврате: {e}")


# ─── Реферальная программа ────────────────────────────────────────────────────

@router.message(Command("ref"))
@router.message(F.text == "🔗 Реферальная программа")
async def cmd_ref(message: Message, user: User) -> None:
    """Показывает реферальную ссылку и описание программы"""
    if user.role == "editor":
        return
    bot_info = await message.bot.get_me()
    ref_link = f"https://t.me/{bot_info.username}?start=ref_{message.from_user.id}"

    async with AsyncSessionLocal() as session:
        stats = await get_referral_stats(session, user.id)

    await message.answer(
        f"🔗 <b>Реферальная программа</b>\n\n"
        f"Приглашай друзей и получай бонусы:\n"
        f"• <b>200 руб.</b> — когда приглашённый оплатит <b>первую</b> работу\n"
        f"• <b>5%</b> от суммы — за каждую <b>следующую</b> работу\n\n"
        f"Твоя реферальная ссылка:\n"
        f"<code>{ref_link}</code>\n\n"
        f"👥 Приглашено: <b>{stats['referrals_count']}</b> чел.\n"
        f"💰 Заработано всего: <b>{stats['total_earned']:.0f} руб.</b>\n"
        f"💳 Текущий баланс: <b>{stats['balance']:.0f} руб.</b>",
        parse_mode="HTML",
    )


@router.message(Command("balance"))
@router.message(F.text == "💰 Мой баланс")
async def cmd_balance(message: Message, user: User) -> None:
    """Показывает реферальный баланс и историю начислений"""
    if user.role == "editor":
        return
    async with AsyncSessionLocal() as session:
        stats = await get_referral_stats(session, user.id)

    bonus_lines = []
    for bonus in stats["bonuses"]:
        bonus_type_text = "первый заказ" if bonus.bonus_type == "first_order" else "повторный заказ"
        bonus_lines.append(
            f"  +{bonus.amount:.0f} руб. ({bonus_type_text}, заказ #{bonus.order_id})"
        )

    bonuses_text = "\n".join(bonus_lines) if bonus_lines else "  Пока нет начислений"

    # Кнопку вывода показываем только студентам
    from bot.keyboards.student_kb import withdrawal_kb
    kb = withdrawal_kb() if user.role == "student" else None

    await message.answer(
        f"💰 <b>Реферальный баланс</b>\n\n"
        f"Текущий баланс: <b>{stats['balance']:.0f} руб.</b>\n"
        f"Всего заработано: <b>{stats['total_earned']:.0f} руб.</b>\n"
        f"Приглашено друзей: <b>{stats['referrals_count']}</b>\n\n"
        f"📋 Последние начисления:\n{bonuses_text}",
        reply_markup=kb,
        parse_mode="HTML",
    )


# ─── FSM: Вывод реферального баланса ─────────────────────────────────────────

@router.callback_query(F.data == "request_withdrawal")
async def cb_request_withdrawal(callback: CallbackQuery, user: User, state: FSMContext) -> None:
    """Точка входа: кнопка «💸 Вывести средства»"""
    if user.role != "student":
        await callback.answer("Вывод доступен только студентам.", show_alert=True)
        return

    async with AsyncSessionLocal() as session:
        stats = await get_referral_stats(session, user.id)

    balance = stats["balance"]
    if balance < 1000:
        await callback.answer(
            f"Недостаточно средств. Минимум для вывода — 1000 руб.\nВаш баланс: {balance:.0f} руб.",
            show_alert=True,
        )
        return

    await state.set_state(WithdrawalStates.entering_amount)
    await state.update_data(ref_balance=balance)
    await callback.message.answer(
        f"💸 <b>Вывод средств</b>\n\n"
        f"Ваш баланс: <b>{balance:.0f} руб.</b>\n\n"
        f"Введите сумму для вывода (минимум <b>1000 руб.</b>):",
        parse_mode="HTML",
    )
    await callback.answer()


@router.message(WithdrawalStates.entering_amount, F.text)
async def wd_enter_amount(message: Message, state: FSMContext, user: User) -> None:
    """Шаг 1: студент ввёл сумму"""
    from datetime import timedelta

    text = (message.text or "").strip().replace(" ", "").replace(",", ".")
    if not text.replace(".", "").isdigit():
        await message.answer("Введите число, например: <code>1500</code>", parse_mode="HTML")
        return

    amount = float(text)
    data = await state.get_data()
    balance = data.get("ref_balance", 0.0)

    if amount < 1000:
        await message.answer("Минимальная сумма вывода — <b>1000 руб.</b> Попробуйте ещё раз:", parse_mode="HTML")
        return

    if amount > balance:
        await message.answer(
            f"Сумма превышает ваш баланс (<b>{balance:.0f} руб.</b>). Введите меньшую сумму:",
            parse_mode="HTML",
        )
        return

    # Проверка cooldown: раз в 30 дней
    async with AsyncSessionLocal() as session:
        last = await get_last_withdrawal_request(session, user.id)

    if last is not None:
        from datetime import timedelta
        cooldown_end = last.created_at + timedelta(days=30)
        if datetime.utcnow() < cooldown_end:
            days_left = (cooldown_end - datetime.utcnow()).days + 1
            await message.answer(
                f"❌ Вывод доступен раз в 30 дней.\n"
                f"Следующий вывод будет доступен через <b>{days_left} дн.</b>",
                parse_mode="HTML",
            )
            await state.clear()
            return

    await state.update_data(wd_amount=amount)
    await state.set_state(WithdrawalStates.waiting_card)
    await message.answer(
        f"✅ Сумма: <b>{amount:.0f} руб.</b>\n\n"
        f"Введите <b>номер карты</b> для перевода\n"
        f"(16 цифр без пробелов, например: <code>4111111111111111</code>):",
        parse_mode="HTML",
    )


@router.message(WithdrawalStates.waiting_card, F.text)
async def wd_enter_card(message: Message, state: FSMContext) -> None:
    """Шаг 2: студент ввёл реквизиты карты"""
    card = (message.text or "").strip().replace(" ", "")

    if not (10 <= len(card) <= 20 and card.isdigit()):
        await message.answer(
            "Введите корректный номер карты (от 10 до 20 цифр без пробелов):"
        )
        return

    await state.update_data(wd_card=card)
    data = await state.get_data()
    amount = data.get("wd_amount", 0.0)

    # Показываем карту маскированной
    masked = card[:4] + " **** **** " + card[-4:]

    from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton
    from aiogram.utils.keyboard import InlineKeyboardBuilder
    kb = InlineKeyboardBuilder()
    kb.row(
        InlineKeyboardButton(text="✅ Подтвердить", callback_data="confirm_withdrawal"),
        InlineKeyboardButton(text="❌ Отмена", callback_data="cancel_withdrawal"),
    )

    await state.set_state(WithdrawalStates.confirm)
    await message.answer(
        f"📋 <b>Подтвердите заявку на вывод</b>\n\n"
        f"💰 Сумма: <b>{amount:.0f} руб.</b>\n"
        f"💳 Карта: <b>{masked}</b>\n\n"
        f"Деньги поступят в течение 1–3 рабочих дней после одобрения.",
        reply_markup=kb.as_markup(),
        parse_mode="HTML",
    )


@router.callback_query(F.data == "confirm_withdrawal", WithdrawalStates.confirm)
async def cb_confirm_withdrawal(callback: CallbackQuery, state: FSMContext, user: User, bot: Bot) -> None:
    """Шаг 3: подтверждение — создаём заявку"""
    data = await state.get_data()
    amount = data.get("wd_amount", 0.0)
    card = data.get("wd_card", "")

    try:
        async with AsyncSessionLocal() as session:
            wd = await create_withdrawal_request(session, user.id, amount, card)
    except ValueError as e:
        await state.clear()
        await callback.message.edit_text(
            f"❌ <b>Не удалось создать заявку:</b> {e}\n\n"
            f"Проверьте баланс — возможно средства уже выведены.",
            parse_mode="HTML",
        )
        return

    await state.clear()

    await callback.message.edit_text(
        f"✅ <b>Заявка на вывод #{wd.id} отправлена!</b>\n\n"
        f"Сумма: <b>{amount:.0f} руб.</b>\n"
        f"Ожидайте перевода в течение 1–3 рабочих дней.",
        parse_mode="HTML",
    )

    # Уведомляем администратора
    from bot.keyboards.admin_kb import withdrawal_action_kb
    masked = card[:4] + " **** **** " + card[-4:]
    try:
        await bot.send_message(
            chat_id=settings.ADMIN_TG_ID,
            text=(
                f"💸 <b>Новая заявка на вывод #{wd.id}</b>\n\n"
                f"👤 Студент: @{user.username or '—'} ({user.full_name or '—'})\n"
                f"💰 Сумма: <b>{amount:.0f} руб.</b>\n"
                f"💳 Карта: <b>{masked}</b>"
            ),
            reply_markup=withdrawal_action_kb(wd.id),
            parse_mode="HTML",
        )
    except Exception as e:
        logger.error(f"Ошибка уведомления о заявке на вывод: {e}")

    await callback.answer()


@router.callback_query(F.data == "cancel_withdrawal")
async def cb_cancel_withdrawal(callback: CallbackQuery, state: FSMContext) -> None:
    await state.clear()
    await callback.message.edit_text("❌ Вывод средств отменён.")
    await callback.answer()


# ─── /email и /phone — для чеков ЮКассы 54-ФЗ ───────────────────────────────

import re
EMAIL_RE = re.compile(r"^[\w.+-]+@[\w-]+\.[\w.-]+$")


@router.message(Command("email"))
async def cmd_email(message: Message, state: FSMContext, user: User) -> None:
    """Запрос email пользователя для чеков ЮКассы (54-ФЗ)."""
    from bot.states.order import EmailStates
    await state.set_state(EmailStates.waiting_email)
    current = f"\n\nТекущий email: <code>{user.email}</code>" if user.email else ""
    await message.answer(
        f"📧 <b>Введите ваш email</b>\n\n"
        f"Он нужен для отправки чека после оплаты (требование 54-ФЗ).{current}\n\n"
        f"Пример: <code>ivan@example.com</code>\n"
        f"Отмена: /cancel",
        parse_mode="HTML",
    )


@router.message(Command("cancel"))
async def cmd_cancel(message: Message, state: FSMContext) -> None:
    cur = await state.get_state()
    if cur:
        await state.clear()
        await message.answer("Отменено.")


# Импортируем EmailStates на уровне модуля для FSM-фильтра
from bot.states.order import EmailStates as _EmailStates


@router.message(_EmailStates.waiting_email, F.text)
async def step_email_input(message: Message, state: FSMContext, user: User) -> None:
    text = (message.text or "").strip()

    if text.startswith("/"):
        # Команда — выходим из состояния
        await state.clear()
        return

    if not EMAIL_RE.match(text):
        await message.answer(
            "⚠️ Похоже, это не email. Введите корректный адрес или /cancel:",
        )
        return

    from db.crud import update_user_email
    async with AsyncSessionLocal() as session:
        await update_user_email(session, message.from_user.id, text)

    data = await state.get_data()
    pay_order_id = data.get("pay_after_email_order_id")
    await state.clear()

    await message.answer(
        f"✅ Email <code>{text}</code> сохранён.\n"
        f"Теперь вы будете получать чеки по оплатам.",
        parse_mode="HTML",
    )

    # Если email запрашивали перед оплатой — теперь создаём ссылку и шлём кнопку.
    if pay_order_id:
        from bot.keyboards.student_kb import preview_payment_kb
        confirmation_url, err = await _ensure_payment_url(pay_order_id)
        if confirmation_url:
            async with AsyncSessionLocal() as session:
                order = await get_order(session, pay_order_id)
                price = (order.price or 0.0) if order else 0.0
                revisions_used = (order.revisions_used or 0) if order else 0
            can_rework = revisions_used < MAX_REVISIONS
            rework_note = (
                "\n\nЕсли что-то нужно поправить — нажмите «✏️ На доработку»."
                if can_rework
                else f"\n\n⚠️ Бесплатные правки исчерпаны ({revisions_used}/{MAX_REVISIONS})."
            )
            await message.answer(
                f"💳 <b>Оплата — заказ #{pay_order_id}</b>\n\n"
                f"Сумма: <b>{price:.0f} ₽</b>{rework_note}",
                reply_markup=preview_payment_kb(pay_order_id, confirmation_url, show_rework=can_rework),
                parse_mode="HTML",
            )
        elif err and err != "NO_EMAIL":
            await message.answer(f"⚠️ {err}")


# ─── /revision — запрос правки клиентом ─────────────────────────────────────

@router.message(Command("revision"))
async def cmd_revision(message: Message, user: User) -> None:
    """Команда `/revision <id>` — подсказка как использовать через /my_orders."""
    args = (message.text or "").split(maxsplit=1)
    if len(args) < 2 or not args[1].strip().isdigit():
        await message.answer(
            "✏️ <b>Запрос правки</b>\n\n"
            "Чтобы запросить правку, откройте /my_orders и нажмите "
            "<b>«✏️ Запросить правку»</b> под нужным заказом.\n\n"
            f"Каждому заказу доступно <b>{MAX_REVISIONS} бесплатные правки</b> "
            f"в течение 3 дней после получения готового файла.",
            parse_mode="HTML",
        )
        return
    # Если передали ID — эмулируем нажатие на кнопку
    order_id = int(args[1].strip())
    # Просто перенаправим логику в callback-обработчик
    await _start_revision_flow(message, user, order_id)


@router.callback_query(F.data.startswith("revision_") & ~F.data.startswith("revision_pick_") & ~F.data.startswith("revision_cancel"))
async def cb_revision_request(callback: CallbackQuery, user: User, state: FSMContext) -> None:
    """Студент нажал «✏️ Запросить правку» под DONE-заказом."""
    order_id_str = callback.data.replace("revision_", "")
    if not order_id_str.isdigit():
        await callback.answer("Некорректный ID.", show_alert=True)
        return
    order_id = int(order_id_str)
    await _start_revision_flow(callback.message, user, order_id, state=state)
    await callback.answer()


async def _start_revision_flow(message: Message, user: User, order_id: int, state: FSMContext = None) -> None:
    """Запускает FSM сбора описания правки."""
    # Проверим что заказ принадлежит юзеру и статус DONE
    async with AsyncSessionLocal() as session:
        order = await get_order(session, order_id)
    if order is None or order.user_id != user.id:
        await message.answer("⚠️ Заказ не найден или принадлежит другому пользователю.")
        return

    status_val = order.status.value if hasattr(order.status, "value") else order.status
    if status_val != OrderStatus.DONE.value:
        await message.answer(
            "⚠️ Правки доступны только для готовых заказов (статус «Выполнен»).\n"
            "Если заказ ещё в работе — дождитесь готового файла."
        )
        return

    used = order.revisions_used or 0
    if used >= MAX_REVISIONS:
        await message.answer(
            f"⚠️ Бесплатные правки исчерпаны ({used}/{MAX_REVISIONS}).\n\n"
            f"Для дополнительных правок свяжитесь с поддержкой /support."
        )
        return

    # Сохраним ID в FSM и спросим описание
    if state is None:
        # Если вызвано не из callback — получим state через bot
        return
    await state.set_state(RevisionStates.waiting_description)
    await state.update_data(revision_order_id=order_id)
    await message.answer(
        f"✏️ <b>Правка заказа #{order_id}</b>\n\n"
        f"Правок использовано: <b>{used}/{MAX_REVISIONS}</b>\n\n"
        f"Опишите подробно, что именно нужно исправить — редактор сразу возьмёт в работу. "
        f"Минимум 10 символов.\n\n"
        f"Отмена: /cancel",
        parse_mode="HTML",
    )


@router.message(RevisionStates.waiting_description, F.text)
async def revision_description_received(message: Message, state: FSMContext, user: User, bot: Bot) -> None:
    text = (message.text or "").strip()
    if text.startswith("/cancel"):
        await state.clear()
        await message.answer("Запрос правки отменён.")
        return

    data = await state.get_data()
    order_id = data.get("revision_order_id")
    await state.clear()

    async with AsyncSessionLocal() as session:
        success, msg = await request_revision(session, order_id, user.id, text)
        if success:
            order = await get_order(session, order_id)

    if not success:
        await message.answer(f"⚠️ {msg}")
        return

    used = (order.revisions_used or 0)
    await message.answer(
        f"✅ <b>Правка по заказу #{order_id} отправлена редактору</b>\n\n"
        f"Использовано правок: <b>{used + 1}/{MAX_REVISIONS}</b> "
        f"(счётчик увеличится после получения исправленного файла).\n\n"
        f"Срок выполнения — обычно 24 часа. Уведомление придёт в боте.",
        parse_mode="HTML",
    )

    # Уведомляем редактора
    if settings.EDITOR_TG_ID:
        try:
            # Прикрепляем последний финальный файл (чтобы редактор сразу видел что править)
            from db.crud import get_file
            async with AsyncSessionLocal() as session:
                final_file = await get_file(session, order_id, FileType.FINAL)

            name = f"@{user.username}" if user.username else (user.full_name or f"id{user.tg_id}")
            notice = (
                f"✏️ <b>Запрос правки — заказ #{order_id}</b>\n\n"
                f"👤 Студент: {name}\n"
                f"📝 Тема: {order.topic}\n"
                f"✏️ Правок: <b>{used + 1}/{MAX_REVISIONS}</b>\n\n"
                f"<b>Что нужно исправить:</b>\n<i>{text}</i>\n\n"
                f"Откройте <b>«📋 Мои задания»</b> чтобы загрузить исправленный .docx."
            )
            await bot.send_message(
                chat_id=settings.EDITOR_TG_ID,
                text=notice,
                parse_mode="HTML",
            )
            # Прикрепляем последний финальный файл
            if final_file and final_file.tg_file_id:
                try:
                    await bot.send_document(
                        chat_id=settings.EDITOR_TG_ID,
                        document=final_file.tg_file_id,
                        caption=f"📄 Текущий финал заказа #{order_id} (его нужно исправить)",
                    )
                except Exception as e:
                    logger.error(f"Не удалось переслать final-файл редактору: {e}")
        except Exception as e:
            logger.error(f"Не удалось уведомить редактора о правке: {e}")


# ─── Просмотр превью и получение ссылки на оплату из /my_orders ──────────────

PAY_REDIRECT_BASE = f"{settings.PUBLIC_URL}/pay"


async def _ensure_payment_url(order_id: int) -> tuple[str | None, str | None]:
    """Возвращает (short_url, error_msg).
    Если для заказа уже есть pending платёж — возвращает короткий /pay/{id}.
    Иначе создаёт новый через Robokassa и сохраняет в БД.

    Используется короткий URL потому что Telegram имеет лимит на длину
    URL в inline-кнопке (~256 символов), а Robokassa URL с Receipt = ~700.
    """
    from db.models import Payment, OrderStatus as OS
    from db.crud import create_payment
    from services.payment import create_payment_url
    from sqlalchemy import select

    async with AsyncSessionLocal() as session:
        order = await get_order(session, order_id)
        if order is None:
            return None, "Заказ не найден"

        order_status_val = order.status.value if hasattr(order.status, "value") else order.status
        if order_status_val not in (OS.PREVIEW_SENT.value, OS.AWAITING_PAYMENT.value):
            return None, f"Оплата сейчас недоступна (статус: {order_status_val})"

        price = order.price or 0.0
        if price <= 0:
            return None, "Цена заказа не установлена"

        student_email = order.user.email if order.user else None
        # Без email чек 54-ФЗ некуда отправить — не создаём платёж, сигналим вызывающему.
        if not student_email:
            return None, "NO_EMAIL"

        r = await session.execute(
            select(Payment).where(Payment.order_id == order_id, Payment.status == "pending")
            .order_by(Payment.created_at.desc()).limit(1)
        )
        pay = r.scalar_one_or_none()
        if pay and pay.confirmation_url:
            return f"{PAY_REDIRECT_BASE}/{pay.id}", None

    # Создаём новый платёж через Robokassa
    try:
        payment_result = await create_payment_url(
            order_id=order_id,
            amount=price,
            description=f"Консультация по учебной работе №{order_id}",
            customer_email=student_email,
        )
        confirmation_url = payment_result["confirmation_url"]
        async with AsyncSessionLocal() as session:
            new_pay = await create_payment(
                session=session, order_id=order_id, amount=price,
                payment_id=payment_result["payment_id"],
                confirmation_url=confirmation_url,
            )
            await update_order_status(session, order_id, OS.AWAITING_PAYMENT)
            new_pay_id = new_pay.id if new_pay else None
        if new_pay_id:
            return f"{PAY_REDIRECT_BASE}/{new_pay_id}", None
        return confirmation_url, None  # fallback
    except Exception as e:
        logger.error(f"Не удалось создать платёж для заказа #{order_id}: {e}", exc_info=True)
        return None, "Не удалось создать ссылку на оплату. Напишите в /support."


async def _prompt_email_then_pay(message: Message, state: FSMContext, order_id: int) -> None:
    """Просит email для чека (54-ФЗ) и запоминает заказ, который надо оплатить
    после ввода email. Резюме — в step_email_input."""
    await state.set_state(_EmailStates.waiting_email)
    await state.update_data(pay_after_email_order_id=order_id)
    await message.answer(
        "📧 <b>Нужен email для чека</b>\n\n"
        "По закону (54-ФЗ) чек об оплате отправляется на email. "
        "Введите ваш адрес — и сразу пришлём ссылку на оплату.\n\n"
        "Пример: <code>ivan@example.com</code>\nОтмена: /cancel",
        parse_mode="HTML",
    )


@router.callback_query(F.data.startswith("view_preview_"))
async def cb_view_preview(cb: CallbackQuery, user: User, state: FSMContext) -> None:
    """Пересылает студенту PDF-превью + ссылку на оплату (создаёт если нет)."""
    try:
        order_id = int(cb.data.removeprefix("view_preview_"))
    except ValueError:
        await cb.answer("Неверный ID заказа", show_alert=True)
        return

    from aiogram.types import FSInputFile
    from db.models import File as FileModel, FileType
    from sqlalchemy import select
    import os

    async with AsyncSessionLocal() as session:
        order = await get_order(session, order_id)
        if order is None or order.user_id != user.id:
            await cb.answer("Заказ не найден", show_alert=True)
            return
        price = order.price or 0.0
        student_email = order.user.email if order.user else None
        revisions_used = order.revisions_used or 0
        can_rework = revisions_used < MAX_REVISIONS

        r = await session.execute(
            select(FileModel)
            .where(FileModel.order_id == order_id, FileModel.file_type == FileType.PREVIEW.value)
            .order_by(FileModel.uploaded_at.desc()).limit(1)
        )
        preview = r.scalar_one_or_none()

    if preview is None or not preview.local_path or not os.path.exists(preview.local_path):
        await cb.answer("Превью пока не готово. Напишите в /support.", show_alert=True)
        return

    await cb.answer("Отправляем превью...")

    # 1. PDF превью
    await cb.message.answer_document(
        document=FSInputFile(preview.local_path),
        caption=(
            f"👀 <b>Превью — заказ #{order_id}</b>\n\n"
            f"Версия с водяным знаком. После оплаты получите чистый файл."
        ),
        parse_mode="HTML",
    )

    # 2. Ссылка на оплату — создаём при необходимости
    confirmation_url, err = await _ensure_payment_url(order_id)
    if err == "NO_EMAIL":
        await _prompt_email_then_pay(cb.message, state, order_id)
        return
    if confirmation_url:
        rework_note = (
            "\n\nЕсли что-то нужно поправить — нажмите «✏️ На доработку»."
            if can_rework
            else f"\n\n⚠️ Бесплатные правки исчерпаны ({revisions_used}/{MAX_REVISIONS})."
        )
        await cb.message.answer(
            f"💳 <b>Оплата — заказ #{order_id}</b>\n\n"
            f"Сумма: <b>{price:.0f} ₽</b>{rework_note}",
            reply_markup=preview_payment_kb(order_id, confirmation_url, show_rework=can_rework),
            parse_mode="HTML",
        )
    else:
        await cb.message.answer(f"⚠️ {err or 'Ошибка получения ссылки на оплату'}")


@router.callback_query(F.data.startswith("get_payment_"))
async def cb_get_payment(cb: CallbackQuery, user: User, state: FSMContext) -> None:
    """Создаёт ссылку на оплату через Robokassa (если ещё нет) и отдаёт студенту."""
    try:
        order_id = int(cb.data.removeprefix("get_payment_"))
    except ValueError:
        await cb.answer("Неверный ID заказа", show_alert=True)
        return

    async with AsyncSessionLocal() as session:
        order = await get_order(session, order_id)
        if order is None or order.user_id != user.id:
            await cb.answer("Заказ не найден", show_alert=True)
            return
        price = order.price or 0.0
        revisions_used = order.revisions_used or 0
        can_rework = revisions_used < MAX_REVISIONS

    await cb.answer()

    confirmation_url, err = await _ensure_payment_url(order_id)
    if err == "NO_EMAIL":
        await _prompt_email_then_pay(cb.message, state, order_id)
        return
    if not confirmation_url:
        await cb.message.answer(f"⚠️ {err or 'Не удалось создать ссылку на оплату'}")
        return

    rework_note = (
        "\n\nЕсли что-то нужно поправить — нажмите «✏️ На доработку»."
        if can_rework
        else f"\n\n⚠️ Бесплатные правки исчерпаны ({revisions_used}/{MAX_REVISIONS})."
    )
    await cb.message.answer(
        f"💳 <b>Оплата — заказ #{order_id}</b>\n\n"
        f"Сумма: <b>{price:.0f} ₽</b>{rework_note}",
        reply_markup=preview_payment_kb(order_id, confirmation_url, show_rework=can_rework),
        parse_mode="HTML",
    )


# ─── На доработку (запрос правки превью до оплаты) ──────────────────────────

@router.callback_query(F.data.startswith("preview_rework_"))
async def cb_preview_rework_start(cb: CallbackQuery, state: FSMContext, user: User) -> None:
    """Студент нажал «✏️ На доработку» — спрашиваем что нужно поправить."""
    try:
        order_id = int(cb.data.removeprefix("preview_rework_"))
    except ValueError:
        await cb.answer("Неверный ID заказа", show_alert=True)
        return

    async with AsyncSessionLocal() as session:
        order = await get_order(session, order_id)
        if order is None or order.user_id != user.id:
            await cb.answer("Заказ не найден", show_alert=True)
            return
        status_val = order.status.value if hasattr(order.status, "value") else order.status

    # Разрешаем только до оплаты (на стадии превью или ожидания оплаты)
    if status_val not in (OrderStatus.PREVIEW_SENT.value, OrderStatus.AWAITING_PAYMENT.value):
        await cb.answer(
            f"Доработка возможна только до оплаты (сейчас: {status_val})",
            show_alert=True,
        )
        return

    await cb.answer()
    await state.set_state(PreviewReworkStates.waiting_comment)
    await state.update_data(rework_order_id=order_id)
    await cb.message.answer(
        f"✏️ <b>Доработка превью — заказ #{order_id}</b>\n\n"
        f"Опишите, что нужно поправить (минимум 5 символов).\n"
        f"Редактор получит ваш комментарий и переделает превью.\n\n"
        f"Чтобы отменить — отправьте /cancel",
        parse_mode="HTML",
        reply_markup=remove_kb(),
    )


@router.message(PreviewReworkStates.waiting_comment, F.text == "/cancel")
async def preview_rework_cancel(message: Message, state: FSMContext, user: User) -> None:
    await state.clear()
    await message.answer(
        "Запрос на доработку отменён.",
        reply_markup=main_menu_kb(user.role),
    )


@router.message(PreviewReworkStates.waiting_comment, F.text)
async def preview_rework_collect(message: Message, state: FSMContext, user: User) -> None:
    from sqlalchemy import update
    from db.models import Order as OrderModel

    text = (message.text or "").strip()
    if len(text) < 5:
        await message.answer("Слишком короткий комментарий. Минимум 5 символов.")
        return

    data = await state.get_data()
    order_id = data.get("rework_order_id")
    if not order_id:
        await state.clear()
        await message.answer("Ошибка состояния. Попробуйте снова.", reply_markup=main_menu_kb(user.role))
        return

    await state.clear()

    # Проверяем лимит бесплатных правок (счётчик revisions_used, лимит MAX_REVISIONS=2)
    async with AsyncSessionLocal() as session:
        order = await get_order(session, order_id)
        if order is None or order.user_id != user.id:
            await message.answer("Заказ не найден.", reply_markup=main_menu_kb(user.role))
            return

        used = order.revisions_used or 0
        if used >= MAX_REVISIONS:
            await message.answer(
                f"⚠️ Бесплатные правки исчерпаны ({used}/{MAX_REVISIONS}).\n"
                f"Напишите в /support — обсудим платную доработку.",
                reply_markup=main_menu_kb(user.role),
            )
            return

        # Сохраняем комментарий + инкремент счётчика + статус → EDITING
        # (чтобы заказ вернулся в очередь редактора и подсветился в /editor).
        await session.execute(
            update(OrderModel)
            .where(OrderModel.id == order_id)
            .values(
                revision_comment=text,
                revisions_used=OrderModel.revisions_used + 1,
                status=OrderStatus.EDITING.value,
            )
        )
        await session.commit()
        new_used = used + 1
        remaining = MAX_REVISIONS - new_used

    # Уведомление студенту
    await message.answer(
        f"✅ Запрос на доработку заказа #{order_id} отправлен.\n\n"
        f"Использовано правок: <b>{new_used}/{MAX_REVISIONS}</b> (осталось: {remaining}).\n"
        f"Редактор переделает превью в течение рабочего дня — пришлёт сюда же.",
        parse_mode="HTML",
        reply_markup=main_menu_kb(user.role),
    )

    # Уведомления админу и редактору
    student_label = f"@{user.username}" if user.username else (user.full_name or f"id{user.tg_id}")
    notification = (
        f"✏️ <b>Студент запросил доработку превью</b>\n\n"
        f"Заказ: #{order_id}\n"
        f"Студент: {student_label} (tg_id {user.tg_id})\n"
        f"Правок: {new_used}/{MAX_REVISIONS}\n\n"
        f"<b>Что поправить:</b>\n{text}\n\n"
        f"Откройте /editor/tasks/{order_id} и пересоздайте превью."
    )

    for target_id in (settings.ADMIN_TG_ID, settings.EDITOR_TG_ID):
        if not target_id:
            continue
        try:
            await message.bot.send_message(target_id, notification, parse_mode="HTML")
        except Exception as e:
            logger.error(f"Не удалось уведомить {target_id} о доработке #{order_id}: {e}")


# ─── Catch-all для произвольных сообщений ─────────────────────────────────────
# Должен быть в самом КОНЦЕ файла, чтобы не перекрыть другие обработчики.
# Срабатывает только вне FSM-состояний и не для команд.

@router.message(F.text & ~F.text.startswith("/"))
async def catch_all_unknown(message: Message, state: FSMContext, user: User) -> None:
    """
    Сообщения вне FSM, которые не подошли ни под один другой хендлер.
    Это последний обработчик в student.router — выполняется только если ничего
    выше не сработало.
    """
    # Проверка: если есть активное FSM-состояние, не лезем
    cur = await state.get_state()
    if cur is not None:
        # В FSM могут быть произвольные тексты — там свои обработчики, мы не вмешиваемся.
        return

    if user.role == "student":
        await message.answer(
            "🤖 Не понял команду.\n\n"
            "Используйте кнопки меню или команды:\n"
            "• /new_order — оформить заказ\n"
            "• /my_orders — мои заказы\n"
            "• /help — все команды",
            reply_markup=main_menu_kb(user.role),
        )
