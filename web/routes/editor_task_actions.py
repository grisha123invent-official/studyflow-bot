"""
Действия по заданиям редактора.

POST /editor/tasks/{id}/upload-final           — загрузка финал-файлов (multipart)
POST /editor/tasks/{id}/generate-preview       — генерация PDF с водяным знаком
POST /editor/tasks/{id}/send-preview-to-student — отправка превью студенту + создание платежа
POST /editor/tasks/{id}/submit-revision        — отправка правочного файла студенту (без watermark)
"""
import asyncio
import logging
import os
import re
from typing import List, Optional

from fastapi import APIRouter, Depends, Request, Form, UploadFile, File as FastAPIFile
from fastapi.responses import RedirectResponse
from sqlalchemy import select, update
from sqlalchemy.orm import selectinload

from config import settings
from db.crud import (
    save_file, update_order_status, get_file, get_files_by_type,
    delete_files_by_type, increment_revisions_used,
)
from db.models import Order, OrderStatus, FileType
from db.session import AsyncSessionLocal
from web.audit import log_admin_action
from web.deps_editor import require_editor, csrf_check_editor
from web.flash_editor import set_editor_flash
from web.notifications import send_document_telegram
from web.security.rate_limit import limiter, get_client_ip

logger = logging.getLogger("editor.task_actions")
router = APIRouter()


# ─── Валидация ───────────────────────────────────────────────────────────────

ALLOWED_FINAL_EXTENSIONS = {".docx", ".pptx", ".pdf"}
MAX_FILE_SIZE = 20 * 1024 * 1024  # 20 МБ
MAX_FINAL_FILES = 5


def _secure_filename(filename: str) -> str:
    name = os.path.basename(filename or "")
    name = re.sub(r"[^\w\-. ]", "", name).strip()
    name = name.replace(" ", "_")
    if len(name) > 100:
        base, ext = os.path.splitext(name)
        name = base[:90] + ext
    return name or "file"


def _redirect_to_task(order_id: int, level: str, message: str) -> RedirectResponse:
    response = RedirectResponse(f"/editor/tasks/{order_id}", status_code=303)
    set_editor_flash(response, level, message)
    return response


# ─── Upload final ────────────────────────────────────────────────────────────

@router.post("/editor/tasks/{order_id}/upload-final")
@limiter.limit("10/minute")
async def upload_final(
    request: Request,
    order_id: int,
    csrf_token: str = Form(...),
    files: List[UploadFile] = FastAPIFile(...),
    editor_tg_id: int = Depends(require_editor),
    _csrf: None = Depends(csrf_check_editor),
):
    # Проверка заказа
    async with AsyncSessionLocal() as session:
        r = await session.execute(select(Order).where(Order.id == order_id))
        order = r.scalar_one_or_none()
        if order is None:
            return _redirect_to_task(order_id, "err", f"Задание #{order_id} не найдено")
        if order.status != OrderStatus.EDITING.value:
            return _redirect_to_task(order_id, "err",
                                      f"Загрузка финала разрешена только в статусе EDITING (сейчас {order.status})")

    # Валидация файлов
    if not files:
        return _redirect_to_task(order_id, "err", "Загрузите хотя бы один файл")
    if len(files) > MAX_FINAL_FILES:
        return _redirect_to_task(order_id, "err", f"Максимум {MAX_FINAL_FILES} файлов")

    file_data = []
    for f in files:
        ext = os.path.splitext(f.filename or "")[1].lower()
        if ext not in ALLOWED_FINAL_EXTENSIONS:
            return _redirect_to_task(order_id, "err",
                                      f"Разрешены .docx, .pptx и .pdf (получено: {ext})")
        content = await f.read()
        if len(content) > MAX_FILE_SIZE:
            return _redirect_to_task(order_id, "err",
                                      f"Файл «{f.filename}» больше 20 МБ")
        if len(content) == 0:
            return _redirect_to_task(order_id, "err", f"Файл «{f.filename}» пустой")
        file_data.append((_secure_filename(f.filename or "final"), content))

    # Сохранение
    upload_dir = os.path.abspath(settings.UPLOADS_DIR)
    os.makedirs(upload_dir, exist_ok=True)

    saved = []
    async with AsyncSessionLocal() as session:
        # Новая загрузка финалов = полная замена набора: удаляем прежние финалы
        # и устаревшие превью, чтобы после правки старые версии не накапливались
        # и не «слипались» с новыми при склейке превью.
        await delete_files_by_type(session, order_id, FileType.FINAL)
        await delete_files_by_type(session, order_id, FileType.PREVIEW)
        for i, (safe_name, content) in enumerate(file_data):
            local_path = os.path.join(upload_dir, f"order_{order_id}_final_{i}_{safe_name}")
            try:
                with open(local_path, "wb") as out:
                    out.write(content)
            except OSError as e:
                logger.error(f"Save failed {local_path}: {e}")
                return _redirect_to_task(order_id, "err", f"Не удалось сохранить «{safe_name}»")
            await save_file(
                session, order_id, FileType.FINAL,
                filename=safe_name, local_path=local_path,
            )
            saved.append(safe_name)

    await log_admin_action(
        admin_tg_id=editor_tg_id, action="editor_upload_final",
        target_type="order", target_id=order_id,
        ip_address=get_client_ip(request),
        user_agent=request.headers.get("user-agent", ""),
        details={"file_count": len(saved)},
    )

    return _redirect_to_task(
        order_id, "ok",
        f"Загружено {len(saved)} файлов. Теперь сгенерируйте превью.",
    )


# ─── Generate preview ────────────────────────────────────────────────────────

@router.post("/editor/tasks/{order_id}/generate-preview")
@limiter.limit("3/minute")
async def generate_preview(
    request: Request,
    order_id: int,
    variant: int = Form(0),
    csrf_token: str = Form(...),
    editor_tg_id: int = Depends(require_editor),
    _csrf: None = Depends(csrf_check_editor),
):
    if variant < 0 or variant > 3:
        variant = 0

    async with AsyncSessionLocal() as session:
        r = await session.execute(select(Order).where(Order.id == order_id))
        order = r.scalar_one_or_none()
        if order is None or order.status != OrderStatus.EDITING.value:
            return _redirect_to_task(order_id, "err",
                                      "Превью можно создавать только для активного задания (EDITING)")

        finals = await get_files_by_type(session, order_id, FileType.FINAL)
        finals = [f for f in finals if f.local_path and os.path.exists(f.local_path)]
        if not finals:
            return _redirect_to_task(order_id, "err",
                                      "Сначала загрузите финальный файл (.docx или .pptx)")
        source_files = [{"local_path": f.local_path} for f in finals]

    # ВАЖНО: водяной знак — тяжёлая LibreOffice/PyMuPDF операция (~10 сек/файл).
    # `_make_previews` сам чистит прежние PREVIEW, гоняет add_watermark в executor
    # для КАЖДОГО финального файла и сохраняет записи PREVIEW в БД.
    try:
        from bot.handlers.editor import _make_previews
        preview_paths = await _make_previews(order_id, source_files, variant=variant)
    except Exception as e:
        logger.error(f"Watermark failed for order #{order_id}: {e}", exc_info=True)
        return _redirect_to_task(order_id, "err",
                                  f"Не удалось создать превью: {e}")

    if not preview_paths:
        return _redirect_to_task(order_id, "err", "Не удалось создать превью")

    await log_admin_action(
        admin_tg_id=editor_tg_id, action="editor_generate_preview",
        target_type="order", target_id=order_id,
        ip_address=get_client_ip(request),
        user_agent=request.headers.get("user-agent", ""),
        details={"variant": variant},
    )

    return _redirect_to_task(order_id, "ok",
                              f"Превью создано (вариант {variant}). Просмотрите и отправьте студенту.")


# ─── Send preview to student ─────────────────────────────────────────────────

@router.post("/editor/tasks/{order_id}/send-preview-to-student")
@limiter.limit("10/minute")
async def send_preview_to_student(
    request: Request,
    order_id: int,
    csrf_token: str = Form(...),
    editor_tg_id: int = Depends(require_editor),
    _csrf: None = Depends(csrf_check_editor),
):
    async with AsyncSessionLocal() as session:
        r = await session.execute(select(Order).where(Order.id == order_id))
        order = r.scalar_one_or_none()
        if order is None:
            return _redirect_to_task(order_id, "err", "Задание не найдено")
        # Разрешаем повторную отправку из PREVIEW_SENT/AWAITING_PAYMENT —
        # на случай если первая отправка упала (например, ошибка aiogram).
        # `_send_to_student` сам защищает от двойного создания платежа.
        if order.status not in (
            OrderStatus.EDITING.value,
            OrderStatus.PREVIEW_SENT.value,
            OrderStatus.AWAITING_PAYMENT.value,
        ):
            return _redirect_to_task(order_id, "err",
                                      f"Отправить можно только до завершения заказа (сейчас {order.status})")

        preview = await get_file(session, order_id, FileType.PREVIEW)
        if preview is None or not preview.local_path or not os.path.exists(preview.local_path):
            return _redirect_to_task(order_id, "err",
                                      "Сначала сгенерируйте превью")

    # Делегируем существующей функции из бота — она сама создаёт платёж
    # и шлёт превью студенту. Импортируем здесь, чтобы не тащить весь bot/ при загрузке модуля.
    try:
        from aiogram import Bot
        from aiogram.client.default import DefaultBotProperties
        from aiogram.client.session.aiohttp import AiohttpSession
        from aiogram.enums import ParseMode
        from bot.handlers.editor import _send_to_student

        # Увеличенный таймаут: склеенное превью может весить несколько МБ,
        # а канал до Telegram с сервера временами тормозит — дефолтных 60с мало.
        bot = Bot(
            token=settings.BOT_TOKEN,
            session=AiohttpSession(timeout=180),
            default=DefaultBotProperties(parse_mode=ParseMode.HTML),
        )
        try:
            await _send_to_student(bot, order_id)
        finally:
            await bot.session.close()
    except Exception as e:
        logger.error(f"send_preview_to_student failed for #{order_id}: {e}", exc_info=True)
        return _redirect_to_task(order_id, "err",
                                  f"Не удалось отправить студенту: {e}")

    await log_admin_action(
        admin_tg_id=editor_tg_id, action="editor_send_preview",
        target_type="order", target_id=order_id,
        ip_address=get_client_ip(request),
        user_agent=request.headers.get("user-agent", ""),
    )

    return _redirect_to_task(order_id, "ok",
                              "Превью отправлено студенту, ссылка на оплату создана. Задание перешло в PREVIEW_SENT.")


# ─── Submit revision (правочный файл — без watermark) ───────────────────────

@router.post("/editor/tasks/{order_id}/submit-revision")
@limiter.limit("10/minute")
async def submit_revision(
    request: Request,
    order_id: int,
    csrf_token: str = Form(...),
    file: UploadFile = FastAPIFile(...),
    editor_tg_id: int = Depends(require_editor),
    _csrf: None = Depends(csrf_check_editor),
):
    # Валидация: только EDITING + revision_comment не пустой + ЗАКАЗ ОПЛАЧЕН.
    # Pre-payment правки идут через обычный flow upload-final + generate-preview + send-preview.
    async with AsyncSessionLocal() as session:
        r = await session.execute(
            select(Order).options(selectinload(Order.user)).where(Order.id == order_id)
        )
        order = r.scalar_one_or_none()
        if order is None:
            return _redirect_to_task(order_id, "err", "Задание не найдено")
        if order.status != OrderStatus.EDITING.value:
            return _redirect_to_task(order_id, "err", "Правка отправляется только из EDITING")
        if not order.revision_comment:
            return _redirect_to_task(order_id, "err",
                                      "Это не правочное задание (revision_comment пуст)")

        # Проверка оплаты: если не оплачено — отказ, направляем на обычный flow
        from db.models import Payment, PaymentStatus
        pr = await session.execute(
            select(Payment.id).where(
                Payment.order_id == order_id,
                Payment.status == PaymentStatus.PAID.value,
            ).limit(1)
        )
        if pr.scalar_one_or_none() is None:
            return _redirect_to_task(order_id, "err",
                                      "Заказ ещё не оплачен. Загрузите финал и сгенерируйте превью с водяным знаком — НЕ используйте submit-revision до оплаты.")

        student_tg_id = order.user.tg_id if order.user else None

    # Файл
    ext = os.path.splitext(file.filename or "")[1].lower()
    if ext not in ALLOWED_FINAL_EXTENSIONS:
        return _redirect_to_task(order_id, "err", "Разрешены .docx, .pptx и .pdf")
    content = await file.read()
    if len(content) > MAX_FILE_SIZE:
        return _redirect_to_task(order_id, "err", "Файл больше 20 МБ")
    if len(content) == 0:
        return _redirect_to_task(order_id, "err", "Пустой файл")

    safe_name = _secure_filename(file.filename or "revision.docx")
    upload_dir = os.path.abspath(settings.UPLOADS_DIR)
    os.makedirs(upload_dir, exist_ok=True)
    local_path = os.path.join(upload_dir, f"order_{order_id}_revision_{safe_name}")
    try:
        with open(local_path, "wb") as out:
            out.write(content)
    except OSError as e:
        logger.error(f"Save failed {local_path}: {e}")
        return _redirect_to_task(order_id, "err", "Не удалось сохранить файл")

    # Сохраняем как FINAL, инкремент правок, статус DONE, очищаем revision_comment
    async with AsyncSessionLocal() as session:
        await save_file(session, order_id, FileType.FINAL,
                        filename=safe_name, local_path=local_path)
        new_count = await increment_revisions_used(session, order_id)
        await update_order_status(session, order_id, OrderStatus.DONE)
        # Очищаем revision_comment
        await session.execute(
            update(Order).where(Order.id == order_id).values(revision_comment=None)
        )
        await session.commit()

    # Отправляем студенту БЕЗ водяного знака
    if student_tg_id:
        await send_document_telegram(
            student_tg_id, local_path,
            caption=f"✏️ <b>Правка по заказу #{order_id}</b>\n\nПравок использовано: {new_count}/2",
            filename=safe_name,
        )

    await log_admin_action(
        admin_tg_id=editor_tg_id, action="editor_submit_revision",
        target_type="order", target_id=order_id,
        ip_address=get_client_ip(request),
        user_agent=request.headers.get("user-agent", ""),
        details={"revisions_used": new_count},
    )

    return _redirect_to_task(order_id, "ok",
                              f"Правка отправлена студенту. Использовано правок: {new_count}/2. Задание закрыто.")
