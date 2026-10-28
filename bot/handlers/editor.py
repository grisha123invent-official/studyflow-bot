"""
Хендлеры для редактора:
- /start — приветствие + меню
- 📋 Мои задания — список заказов в работе с кнопкой «Начать»
- 🚀 Начать работу → редактор грузит финальные .docx → ✅ Подтвердить
- На «Подтвердить» — создаётся PDF-превью с водяным знаком и отправляется студенту
"""
import os
import logging
from aiogram import Router, F, Bot
from aiogram.filters import CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.types import Message, CallbackQuery, Document

from config import settings
from db.models import OrderStatus, FileType, UserRole
from db.session import AsyncSessionLocal
from db.crud import (
    get_order, update_order_status, save_file, get_file,
    create_payment, get_orders_by_status,
)
from services.watermark import add_watermark_merged
from services.payment import create_payment_url
from bot.keyboards.student_kb import payment_kb
import asyncio
from bot.keyboards.admin_kb import editor_task_kb, editor_collect_kb, editor_main_kb, editor_preview_kb
from bot.states.order import EditorStates
from bot.filters import RoleFilter

logger = logging.getLogger(__name__)
router = Router()


def _is_editor(user) -> bool:
    if user is None:
        return False
    role_val = user.role.value if hasattr(user.role, "value") else user.role
    return role_val == UserRole.EDITOR.value


# ─── /start редактора ────────────────────────────────────────────────────────

@router.message(CommandStart(), RoleFilter("editor"))
async def editor_start_cmd(message: Message, user, state: FSMContext) -> None:
    await state.clear()
    await message.answer(
        "👋 Привет! Ты в панели редактора.\n\n"
        "Нажми <b>📋 Мои задания</b>, чтобы увидеть все активные заказы.",
        reply_markup=editor_main_kb(),
        parse_mode="HTML",
    )


# ─── 📋 Мои задания ──────────────────────────────────────────────────────────

@router.message(F.text.in_({"📋 Мои задания", "📂 Мои заказы"}), RoleFilter("editor"))
async def editor_my_tasks(message: Message, user, state: FSMContext) -> None:
    logger.info(f"editor_my_tasks: tg_id={message.from_user.id} role={getattr(user, 'role', None)}")
    await state.clear()

    async with AsyncSessionLocal() as session:
        orders = await get_orders_by_status(session, OrderStatus.EDITING)

    if not orders:
        await message.answer(
            "📋 Сейчас нет активных заданий.",
            reply_markup=editor_main_kb(),
        )
        return

    await message.answer(
        f"📋 <b>Активные задания ({len(orders)}):</b>",
        parse_mode="HTML",
        reply_markup=editor_main_kb(),
    )

    for order in orders:
        u = order.user
        name = f"@{u.username}" if u.username else (u.full_name or f"id{u.tg_id}")
        is_revision = bool((order.revision_comment or "").strip())

        header = "✏️ <b>ПРАВКА</b>" if is_revision else "📌"
        text = (
            f"{header} <b>Заказ #{order.id}</b>\n"
            f"👤 Студент: {name}\n"
            f"📚 Тип: {order.work_type or '—'}\n"
            f"📝 Тема: {order.topic}\n"
            f"📏 Объём: {order.pages} стр.\n"
            f"📅 Дедлайн: {order.deadline}"
        )
        if is_revision:
            text += (
                f"\n\n<b>Что нужно исправить:</b>\n"
                f"<i>{order.revision_comment}</i>"
            )
        await message.answer(text, reply_markup=editor_task_kb(order.id), parse_mode="HTML")


# ─── 🚀 Начать работу ────────────────────────────────────────────────────────

@router.callback_query(F.data.startswith("editor_start_"))
async def cb_editor_start(callback: CallbackQuery, user, state: FSMContext, bot: Bot) -> None:
    if not _is_editor(user):
        await callback.answer("Нет доступа.", show_alert=True)
        return

    order_id = int(callback.data.split("_")[-1])

    from db.crud import get_all_order_files

    async with AsyncSessionLocal() as session:
        order = await get_order(session, order_id)
        if order is None:
            await callback.answer("Заказ не найден.", show_alert=True)
            return
        all_files = await get_all_order_files(session, order_id)

    await state.set_state(EditorStates.collecting_final)
    await state.update_data(editor_order_id=order_id, editor_files=[])

    # Группируем: файлы от студента и от админа
    student_files = [f for f in all_files if f.file_type == FileType.STUDENT_TEMPLATE.value]
    admin_files = [f for f in all_files if f.file_type == FileType.DRAFT.value]

    chat_id = callback.from_user.id

    async def _send_one(f, caption: str) -> None:
        from aiogram.types import FSInputFile
        filename = (f.filename or "").lower()
        is_photo = filename.endswith((".jpg", ".jpeg", ".png", ".webp")) or filename == "фото.jpg"

        # Пробуем сначала как документ, потом как фото
        sources = []
        if f.tg_file_id:
            sources.append(("tg", f.tg_file_id))
        if f.local_path and os.path.isfile(f.local_path):
            sources.append(("local", FSInputFile(f.local_path)))

        for kind, src in sources:
            try:
                if is_photo:
                    await bot.send_photo(chat_id=chat_id, photo=src, caption=caption)
                else:
                    await bot.send_document(chat_id=chat_id, document=src, caption=caption)
                return
            except Exception as e_first:
                # Если документ — пробуем фото; если фото — пробуем документ
                try:
                    if is_photo:
                        await bot.send_document(chat_id=chat_id, document=src, caption=caption)
                    else:
                        await bot.send_photo(chat_id=chat_id, photo=src, caption=caption)
                    return
                except Exception as e_second:
                    logger.error(f"Файл {f.id} {kind}: {e_first} / {e_second}")
        await callback.message.answer(f"⚠️ Не удалось отправить файл: {f.filename or '—'}")

    if student_files:
        await callback.message.answer(
            f"👤 <b>Файлы от студента ({len(student_files)} шт.):</b>",
            parse_mode="HTML",
        )
        for i, f in enumerate(student_files, 1):
            await _send_one(f, f"👤 От студента {i}/{len(student_files)} — {f.filename or 'файл'}")

    if admin_files:
        await callback.message.answer(
            f"🛠 <b>Файлы от админа ({len(admin_files)} шт.):</b>",
            parse_mode="HTML",
        )
        for i, f in enumerate(admin_files, 1):
            await _send_one(f, f"🛠 От админа {i}/{len(admin_files)} — {f.filename or 'файл'}")

    if not student_files and not admin_files:
        await callback.message.answer(
            f"⚠️ По заказу #{order_id} нет прикреплённых файлов.",
        )

    await callback.message.answer(
        f"📎 <b>Заказ #{order_id} — прикрепи финальный файл</b>\n\n"
        f"Загружай <b>.docx</b>, <b>.pptx</b> (презентация) или <b>.pdf</b> — можно один или несколько.\n"
        f"Когда всё загрузишь — нажми <b>«✅ Подтвердить — отправить студенту»</b>.",
        reply_markup=editor_collect_kb(),
        parse_mode="HTML",
    )
    await callback.answer("Начали!")


# ─── Сбор финальных файлов ───────────────────────────────────────────────────

@router.message(EditorStates.collecting_final, F.document)
async def editor_collect_document(message: Message, state: FSMContext, bot: Bot) -> None:
    doc: Document = message.document
    filename = doc.file_name or "файл"

    if not filename.lower().endswith((".docx", ".pptx", ".pdf")):
        await message.answer(
            "Принимаются файлы <b>.docx</b>, <b>.pptx</b> и <b>.pdf</b>. Загрузи нужный формат.",
            parse_mode="HTML",
        )
        return

    data = await state.get_data()
    order_id = data.get("editor_order_id")
    files = data.get("editor_files", [])

    # Первый файл новой сессии загрузки → ПОЛНАЯ замена набора: удаляем прежние
    # финалы и устаревшие превью этого заказа. Иначе после правки старая версия
    # остаётся в БД и «слипается» с новой при склейке превью.
    if not files:
        from db.crud import delete_files_by_type
        async with AsyncSessionLocal() as session:
            await delete_files_by_type(session, order_id, FileType.FINAL)
            await delete_files_by_type(session, order_id, FileType.PREVIEW)

    os.makedirs(settings.UPLOADS_DIR, exist_ok=True)
    # Индекс в имени — чтобы два файла с одинаковым именем не перезаписали друг друга
    local_path = os.path.join(
        settings.UPLOADS_DIR, f"order_{order_id}_final_{len(files) + 1}_{filename}"
    )

    try:
        file_info = await bot.get_file(doc.file_id)
        await bot.download_file(file_info.file_path, local_path)
    except Exception as e:
        logger.error(f"Ошибка скачивания файла редактора: {e}")
        await message.answer(f"⚠️ Ошибка скачивания файла: {e}")
        return

    async with AsyncSessionLocal() as session:
        await save_file(
            session=session,
            order_id=order_id,
            file_type=FileType.FINAL,
            tg_file_id=doc.file_id,
            filename=filename,
            local_path=local_path,
        )

    files.append({"filename": filename, "local_path": local_path, "tg_file_id": doc.file_id})
    await state.update_data(editor_files=files)

    await message.answer(
        f"📎 Файл <b>{filename}</b> принят ({len(files)} шт.)\n"
        f"Загружай ещё или нажми <b>«✅ Подтвердить»</b>.",
        reply_markup=editor_collect_kb(),
        parse_mode="HTML",
    )


@router.message(EditorStates.collecting_final, F.text == "❌ Отмена")
async def editor_collect_cancel(message: Message, state: FSMContext) -> None:
    await state.clear()
    await message.answer("Отменено.", reply_markup=editor_main_kb())


@router.message(EditorStates.collecting_final, F.text == "✅ Подтвердить — отправить студенту")
async def editor_collect_confirm(message: Message, state: FSMContext, bot: Bot) -> None:
    data = await state.get_data()
    order_id = data.get("editor_order_id")
    files = data.get("editor_files", [])
    await state.clear()

    if not files:
        await message.answer(
            "⚠️ Ты не загрузил ни одного файла. Загрузи хотя бы один .docx, .pptx или .pdf.",
            reply_markup=editor_main_kb(),
        )
        return

    async with AsyncSessionLocal() as session:
        order = await get_order(session, order_id)
        if order is None:
            await message.answer("Заказ не найден.", reply_markup=editor_main_kb())
            return
        student_tg_id = order.user.tg_id
        price = order.price or 0.0
        is_revision = bool((order.revision_comment or "").strip())

        # Правка ПОСЛЕ оплаты (заказ оплачен) → отдаём чистый файл напрямую.
        # Правка ДО оплаты (preview-доработка, не оплачен) → обычный preview-флоу
        # с водяным знаком, БЕЗ чистого файла и БЕЗ инкремента здесь (студент уже
        # инкрементировал при запросе доработки). Различаем по факту оплаты.
        is_paid = False
        if is_revision:
            from sqlalchemy import select as _select
            from db.models import Payment, PaymentStatus
            pr = await session.execute(
                _select(Payment.id).where(
                    Payment.order_id == order_id,
                    Payment.status == PaymentStatus.PAID.value,
                ).limit(1)
            )
            is_paid = pr.scalar_one_or_none() is not None

    # ─── Ветка для ПОСЛЕ-оплатной правки ─────────────────────────────────
    # Заказ оплачен и есть revision_comment: отправляем .docx сразу клиенту
    # без превью и без новой оплаты, инкрементируем счётчик.
    if is_revision and is_paid:
        from db.crud import increment_revisions_used, MAX_REVISIONS
        await message.answer(
            f"✏️ Отправляю исправленные файлы студенту по правке заказа #{order_id} "
            f"({len(files)} шт.)...",
            reply_markup=editor_main_kb(),
        )
        from aiogram.types import FSInputFile
        try:
            for i, fobj in enumerate(files, 1):
                f_tg = fobj.get("tg_file_id")
                f_path = fobj.get("local_path")
                caption = (
                    f"✏️ <b>Правка по заказу #{order_id} готова</b> "
                    f"({i}/{len(files)})\n\nСпасибо, что выбираете нас!"
                )
                if f_tg:
                    document = f_tg
                elif f_path and os.path.isfile(f_path):
                    document = FSInputFile(f_path)
                else:
                    continue
                await _send_document_with_retry(
                    bot,
                    chat_id=student_tg_id,
                    document=document,
                    caption=caption,
                )
        except Exception as e:
            logger.error(f"Ошибка отправки правки студенту: {e}")
            await message.answer(f"⚠️ Ошибка отправки: {e}", reply_markup=editor_main_kb())
            return

        # Инкрементируем счётчик и возвращаем статус DONE
        async with AsyncSessionLocal() as session:
            new_count = await increment_revisions_used(session, order_id)
            # Очищаем revision_comment
            from sqlalchemy import update as sql_update
            from db.models import Order
            await session.execute(
                sql_update(Order).where(Order.id == order_id).values(revision_comment=None)
            )
            await session.commit()
            await update_order_status(session, order_id, OrderStatus.DONE)

        await message.answer(
            f"✅ Правка отправлена студенту. Счётчик правок: <b>{new_count}/{MAX_REVISIONS}</b>",
            reply_markup=editor_main_kb(),
            parse_mode="HTML",
        )
        return
    # ────────────────────────────────────────────────────────────────────

    await message.answer(
        f"⏳ Создаю PDF-превью для заказа #{order_id} ({len(files)} файл(ов))...\n"
        f"Это может занять 10–30 секунд.",
        reply_markup=editor_main_kb(),
    )

    try:
        preview_paths = await _make_previews(order_id, files, variant=0)
    except Exception as e:
        logger.error(f"Ошибка создания превью: {e}")
        await message.answer(f"❌ Ошибка создания PDF: {e}", reply_markup=editor_main_kb())
        return

    if not preview_paths:
        await message.answer(
            "❌ Не удалось создать превью — нет валидных файлов.",
            reply_markup=editor_main_kb(),
        )
        return

    # Отправляем все превью РЕДАКТОРУ для проверки
    try:
        await _send_previews_to_editor(message, order_id, preview_paths, variant=0)
    except Exception as e:
        logger.error(f"Не удалось отправить превью редактору: {e}")
        await message.answer(f"⚠️ Ошибка отправки превью: {e}", reply_markup=editor_main_kb())


async def _make_previews(order_id: int, source_files: list[dict], variant: int) -> list[str]:
    """
    Генерирует ОДНО превью с водяным знаком на весь заказ: каждый исходный файл
    конвертируется в PDF с водяным знаком, затем все склеиваются в один PDF.

    Перед генерацией удаляет прежние PREVIEW-файлы (чтобы при пересоздании
    студенту не ушли устаревшие версии). Дубликаты исходников (один и тот же
    local_path) отбрасываются. Возвращает список из одного пути (склейка) —
    список, чтобы не ломать вызывающий код.

    :param source_files: список dict с ключом ``local_path`` (.docx/.pptx)
    :param variant: вариант расположения водяного знака
    """
    from db.crud import delete_files_by_type

    async with AsyncSessionLocal() as session:
        await delete_files_by_type(session, order_id, FileType.PREVIEW)

    # Дедуп по local_path с сохранением порядка
    seen: set[str] = set()
    paths: list[str] = []
    for sf in source_files:
        src = sf.get("local_path")
        if src and os.path.isfile(src) and src not in seen:
            seen.add(src)
            paths.append(src)
    if not paths:
        return []

    loop = asyncio.get_event_loop()
    merged = await loop.run_in_executor(
        None, lambda: add_watermark_merged(paths, order_id, variant=variant)
    )

    async with AsyncSessionLocal() as session:
        await save_file(
            session=session,
            order_id=order_id,
            file_type=FileType.PREVIEW,
            filename=os.path.basename(merged),
            local_path=merged,
            is_watermarked=True,
        )
    return [merged]


async def _send_previews_to_editor(
    target_message: Message, order_id: int, preview_paths: list[str], variant: int
) -> None:
    """Отправляет все превью редактору; кнопки approve/regen — на последнем."""
    from aiogram.types import FSInputFile

    total = len(preview_paths)
    for i, path in enumerate(preview_paths, 1):
        is_last = i == total
        kb = editor_preview_kb(order_id, variant=variant) if is_last else None
        counter = f" {i}/{total}" if total > 1 else ""
        caption = f"👀 <b>Превью{counter} — заказ #{order_id}</b>"
        if is_last:
            caption += (
                "\n\nПроверь превью. Если всё ок — отправь студенту.\n"
                "Если нужно пересоздать — нажми 🔄."
            )
        await target_message.answer_document(
            document=FSInputFile(path),
            caption=caption,
            reply_markup=kb,
            parse_mode="HTML",
        )


async def _send_document_with_retry(
    bot: Bot, *, chat_id: int, document, caption: str | None = None, attempts: int = 3
):
    """Отправляет документ с ретраями на сетевых таймаутах Telegram.

    Канал до Telegram с сервера временами тормозит (а склеенное превью весит
    несколько МБ), поэтому одиночный таймаут — не повод считать отправку
    проваленной: повторяем с нарастающей паузой.
    """
    from aiogram.exceptions import TelegramNetworkError

    last_exc: Exception | None = None
    for n in range(1, attempts + 1):
        try:
            return await bot.send_document(
                chat_id=chat_id, document=document, caption=caption, parse_mode="HTML"
            )
        except TelegramNetworkError as e:
            last_exc = e
            logger.warning(f"send_document сетевой сбой (попытка {n}/{attempts}): {e}")
            if n < attempts:
                await asyncio.sleep(2 * n)
    raise last_exc  # type: ignore[misc]


async def _send_to_student(bot: Bot, order_id: int) -> None:
    """Отправляет превью и ссылку на оплату студенту.

    Защита от двойной оплаты: если у заказа уже есть платёж со ссылкой,
    переиспользуем его confirmation_url, новый платёж не создаём.
    """
    from sqlalchemy import select
    from aiogram.types import FSInputFile
    from db.models import Payment

    async with AsyncSessionLocal() as session:
        order = await get_order(session, order_id)
        if order is None:
            return
        student_tg_id = order.user.tg_id
        student_email = order.user.email
        price = order.price or 0.0
        order_status_val = order.status.value if hasattr(order.status, "value") else order.status
        from db.crud import MAX_REVISIONS as _MAX_REV
        revisions_used_val = order.revisions_used or 0
        can_rework_val = revisions_used_val < _MAX_REV

        # Ищем существующий платёж (защита от двойной оплаты)
        existing_payment = None
        if order_status_val in (OrderStatus.AWAITING_PAYMENT.value, OrderStatus.DONE.value):
            r = await session.execute(
                select(Payment)
                .where(Payment.order_id == order_id)
                .order_by(Payment.created_at.desc())
                .limit(1)
            )
            existing_payment = r.scalar_one_or_none()

        # Если заказ уже DONE — оплачен, ничего не делаем
        if order_status_val == OrderStatus.DONE.value:
            logger.info(f"Заказ #{order_id} уже DONE, повторная отправка студенту пропущена")
            return

    # Собираем ВСЕ превью-файлы заказа (их может быть несколько)
    from db.crud import get_files_by_type
    async with AsyncSessionLocal() as session:
        previews = await get_files_by_type(session, order_id, FileType.PREVIEW)
    preview_paths = [p.local_path for p in previews if p.local_path and os.path.isfile(p.local_path)]

    if not preview_paths:
        logger.error(f"Нет превью-файлов для отправки студенту по заказу #{order_id}")
        raise RuntimeError("Нет превью-файлов для отправки")

    # Документы превью шлём ТОЛЬКО при первой отправке (статус ещё EDITING).
    # Повторная отправка (resend из PREVIEW_SENT/AWAITING_PAYMENT, напр. после
    # таймаута или двойного клика) НЕ должна слать студенту копии превью заново —
    # иначе он получает дубликаты сообщений. На resend обновим только ссылку оплаты.
    is_first_send = order_status_val == OrderStatus.EDITING.value

    # ВАЖНО: статус обновляем ПОСЛЕ успешной отправки, чтобы при ошибке
    # не оставлять заказ в состоянии «PREVIEW_SENT» без реального сообщения.
    total = len(preview_paths)
    if is_first_send:
        try:
            for i, path in enumerate(preview_paths, 1):
                counter = f" ({i}/{total})" if total > 1 else ""
                caption = f"👀 <b>Превью вашей работы — заказ #{order_id}</b>{counter}"
                if i == 1:
                    caption += (
                        "\n\nОзнакомительная версия с водяным знаком.\n"
                        "После оплаты получите чистый файл."
                    )
                await _send_document_with_retry(
                    bot,
                    chat_id=student_tg_id,
                    document=FSInputFile(path),
                    caption=caption,
                )
        except Exception as e:
            logger.error(f"Не удалось отправить превью студенту: {e}", exc_info=True)
            raise

    # Превью отправлено — обновим статус + очистим revision_comment (если была доработка).
    # Если revision_comment остался не пустым — у студента в /my_orders будет висеть
    # «🔄 На доработке у редактора», что неправильно после отправки нового превью.
    from sqlalchemy import update as sa_update
    from db.models import Order as OrderModel
    async with AsyncSessionLocal() as session:
        if order_status_val not in (OrderStatus.PREVIEW_SENT.value, OrderStatus.AWAITING_PAYMENT.value):
            await update_order_status(session, order_id, OrderStatus.PREVIEW_SENT)
        # Очищаем флаг «на доработке»
        await session.execute(
            sa_update(OrderModel).where(OrderModel.id == order_id).values(revision_comment=None)
        )
        await session.commit()

    payment_url = None
    # Переиспользуем существующий confirmation_url, если есть
    if existing_payment and existing_payment.confirmation_url and existing_payment.status == "pending":
        payment_url = existing_payment.confirmation_url
        logger.info(f"Переиспользуем существующий платёж для заказа #{order_id}")
        # Делаем короткий редирект если у нас payment.id
        if existing_payment and existing_payment.id:
            payment_url = f"{settings.PUBLIC_URL}/pay/{existing_payment.id}"
    elif settings.ROBOKASSA_LOGIN and student_email:
        # Платёж создаём ТОЛЬКО когда есть email студента — иначе чек 54-ФЗ ушёл бы
        # на fallback-адрес, а не клиенту. Без email кнопка «Оплатить» будет
        # callback'ом get_payment_*, который сперва запросит email.
        try:
            payment_result = await create_payment_url(
                order_id=order_id,
                amount=price,
                description=f"Консультация по учебной работе №{order_id}",
                customer_email=student_email,
            )
            full_payment_url = payment_result["confirmation_url"]
            async with AsyncSessionLocal() as session:
                new_pay = await create_payment(
                    session=session, order_id=order_id, amount=price,
                    payment_id=payment_result["payment_id"],
                    confirmation_url=full_payment_url,
                )
                await update_order_status(session, order_id, OrderStatus.AWAITING_PAYMENT)
                # Короткий редирект-URL чтобы влез в Telegram inline-кнопку
                payment_url = f"{settings.PUBLIC_URL}/pay/{new_pay.id}" if new_pay else full_payment_url
        except Exception as e:
            logger.error(f"Ошибка создания платежа: {e}")

    from bot.keyboards.student_kb import preview_payment_kb
    # Если email нет — payment_url=None, кнопка «Оплатить» станет callback'ом,
    # который сперва запросит email (чек 54-ФЗ), потом создаст ссылку.
    email_note = (
        "" if student_email
        else "\n\n📧 Нажмите «Оплатить» — попросим email для чека (требование 54-ФЗ), затем дадим ссылку."
    )
    rework_note = (
        "\n\nЕсли что-то нужно поправить — нажмите «✏️ На доработку»."
        if can_rework_val
        else f"\n\n⚠️ Бесплатные правки исчерпаны ({revisions_used_val}/{_MAX_REV})."
    )
    await bot.send_message(
        chat_id=student_tg_id,
        text=(
            f"💳 <b>Оплата — заказ #{order_id}</b>\n\n"
            f"Сумма: <b>{price:.0f} руб.</b>{email_note}{rework_note}"
        ),
        reply_markup=preview_payment_kb(order_id, payment_url, show_rework=can_rework_val),
        parse_mode="HTML",
    )


@router.callback_query(F.data.startswith("preview_approve_"))
async def cb_preview_approve(callback: CallbackQuery, user, bot: Bot) -> None:
    if not _is_editor(user):
        await callback.answer("Нет доступа.", show_alert=True)
        return

    parts = callback.data.split("_")
    order_id = int(parts[2])

    # Проверяем, что есть хотя бы одно превью
    from db.crud import get_files_by_type
    async with AsyncSessionLocal() as session:
        previews = await get_files_by_type(session, order_id, FileType.PREVIEW)

    has_preview = any(p.local_path and os.path.isfile(p.local_path) for p in previews)
    if not has_preview:
        await callback.answer("Файлы превью не найдены.", show_alert=True)
        return

    await callback.message.edit_reply_markup(reply_markup=None)
    await callback.message.answer("📤 Отправляю студенту...")
    await _send_to_student(bot, order_id)
    await callback.message.answer(f"✅ Заказ #{order_id} отправлен студенту!", reply_markup=editor_main_kb())
    await callback.answer()


@router.callback_query(F.data.startswith("preview_regen_"))
async def cb_preview_regen(callback: CallbackQuery, user, bot: Bot) -> None:
    if not _is_editor(user):
        await callback.answer("Нет доступа.", show_alert=True)
        return

    parts = callback.data.split("_")
    order_id = int(parts[2])
    variant = int(parts[3]) + 1  # следующий вариант расположения

    # Берём ВСЕ финальные файлы для пересоздания превью
    from db.crud import get_files_by_type
    async with AsyncSessionLocal() as session:
        finals = await get_files_by_type(session, order_id, FileType.FINAL)

    source_files = [
        {"local_path": f.local_path, "filename": f.filename}
        for f in finals
        if f.local_path and os.path.isfile(f.local_path)
    ]
    if not source_files:
        await callback.answer("Финальные файлы не найдены.", show_alert=True)
        return

    await callback.message.edit_reply_markup(reply_markup=None)
    await callback.message.answer(
        f"🔄 Пересоздаю превью (вариант {variant}, {len(source_files)} файл(ов))..."
    )

    try:
        preview_paths = await _make_previews(order_id, source_files, variant=variant)
    except Exception as e:
        logger.error(f"Ошибка пересоздания превью: {e}")
        await callback.message.answer(f"❌ Ошибка: {e}")
        await callback.answer()
        return

    if not preview_paths:
        await callback.message.answer("❌ Не удалось пересоздать превью.")
        await callback.answer()
        return

    try:
        await _send_previews_to_editor(callback.message, order_id, preview_paths, variant=variant)
    except Exception as e:
        await callback.message.answer(f"⚠️ Ошибка отправки: {e}")

    await callback.answer()
