"""
Действия в карточке заказа.

POST /admin/orders/{id}/accept          — PENDING → IN_WORK
POST /admin/orders/{id}/reject          — PENDING → CANCELLED (с причиной)
POST /admin/orders/{id}/change-price    — обновить цену (с уведомлением)
POST /admin/orders/{id}/send-to-editor  — multipart: файлы + комментарий → EDITING
"""
import logging
import os
import re
from typing import List, Optional

from fastapi import APIRouter, Depends, Request, Form, UploadFile, File as FastAPIFile
from fastapi.responses import RedirectResponse
from sqlalchemy import select
from sqlalchemy.orm import selectinload

from config import settings
from db.crud import (
    get_order, update_order_status, update_order_price, save_file,
)
from db.models import Order, OrderStatus, FileType
from db.session import AsyncSessionLocal
from web.audit import log_admin_action
from web.deps import require_admin, csrf_check
from web.flash import set_flash
from web.notifications import notify_telegram, send_document_telegram
from web.security.rate_limit import limiter, get_client_ip

logger = logging.getLogger("admin.order_actions")
router = APIRouter()


# ─── Валидация файлов ───────────────────────────────────────────────────────

ALLOWED_EXTENSIONS = {".docx", ".pdf", ".png", ".jpg", ".jpeg", ".txt"}
MAX_FILE_SIZE = 10 * 1024 * 1024  # 10 МБ
MAX_FILES = 5


def _secure_filename(filename: str) -> str:
    """
    Простая санитизация имени файла.
    Убирает path traversal, оставляет только безопасные символы.
    """
    # Убираем path-сегменты
    name = os.path.basename(filename or "")
    # Только буквы/цифры/-/_/. + пробел заменим на _
    name = re.sub(r"[^\w\-. ]", "", name).strip()
    name = name.replace(" ", "_")
    # Лимит длины
    if len(name) > 100:
        base, ext = os.path.splitext(name)
        name = base[:90] + ext
    return name or "file"


def _redirect_to_order(order_id: int, level: str, message: str) -> RedirectResponse:
    response = RedirectResponse(f"/admin/orders/{order_id}", status_code=303)
    set_flash(response, level, message)
    return response


# ─── Accept ──────────────────────────────────────────────────────────────────

@router.post("/admin/orders/{order_id}/accept")
@limiter.limit("30/minute")
async def accept_order(
    request: Request,
    order_id: int,
    csrf_token: str = Form(...),
    admin_tg_id: int = Depends(require_admin),
    _csrf: None = Depends(csrf_check),
):
    async with AsyncSessionLocal() as session:
        order = await get_order(session, order_id)
        if order is None:
            return _redirect_to_order(order_id, "err", f"Заказ #{order_id} не найден")
        if order.status != OrderStatus.PENDING.value:
            return _redirect_to_order(order_id, "err",
                                       f"Можно принять только заказ в статусе PENDING (сейчас {order.status})")

        await update_order_status(session, order_id, OrderStatus.IN_WORK)

        # вытаскиваем user.tg_id для уведомления
        r = await session.execute(select(Order).options(selectinload(Order.user)).where(Order.id == order_id))
        order = r.scalar_one()
        student_tg_id = order.user.tg_id if order.user else None

    if student_tg_id:
        await notify_telegram(
            student_tg_id,
            f"✅ <b>Администратор принял ваш заказ #{order_id}</b>\n\n"
            f"Мы приступили к работе. Скоро вы получите превью.",
        )

    await log_admin_action(
        admin_tg_id=admin_tg_id, action="accept_order",
        target_type="order", target_id=order_id,
        ip_address=get_client_ip(request),
        user_agent=request.headers.get("user-agent", ""),
    )

    return _redirect_to_order(order_id, "ok", f"Заказ #{order_id} принят. Студент уведомлён.")


# ─── Reject ──────────────────────────────────────────────────────────────────

@router.post("/admin/orders/{order_id}/reject")
@limiter.limit("30/minute")
async def reject_order(
    request: Request,
    order_id: int,
    reason: str = Form(...),
    csrf_token: str = Form(...),
    admin_tg_id: int = Depends(require_admin),
    _csrf: None = Depends(csrf_check),
):
    reason_clean = (reason or "").strip()
    if len(reason_clean) < 3:
        return _redirect_to_order(order_id, "err", "Причина отказа должна быть минимум 3 символа")

    async with AsyncSessionLocal() as session:
        order = await get_order(session, order_id)
        if order is None:
            return _redirect_to_order(order_id, "err", f"Заказ #{order_id} не найден")
        if order.status != OrderStatus.PENDING.value:
            return _redirect_to_order(order_id, "err",
                                       f"Отклонить можно только PENDING-заказ (сейчас {order.status})")

        await update_order_status(session, order_id, OrderStatus.CANCELLED)

        r = await session.execute(select(Order).options(selectinload(Order.user)).where(Order.id == order_id))
        order = r.scalar_one()
        student_tg_id = order.user.tg_id if order.user else None

    if student_tg_id:
        await notify_telegram(
            student_tg_id,
            f"❌ <b>Ваш заказ #{order_id} отклонён</b>\n\n"
            f"Причина: {reason_clean}\n\n"
            f"Если у вас есть вопросы — напишите в /support.",
        )

    await log_admin_action(
        admin_tg_id=admin_tg_id, action="reject_order",
        target_type="order", target_id=order_id,
        ip_address=get_client_ip(request),
        user_agent=request.headers.get("user-agent", ""),
        details={"reason": reason_clean},
    )

    return _redirect_to_order(order_id, "ok", f"Заказ #{order_id} отклонён. Студент уведомлён.")


# ─── Change price ────────────────────────────────────────────────────────────

@router.post("/admin/orders/{order_id}/change-price")
@limiter.limit("30/minute")
async def change_price(
    request: Request,
    order_id: int,
    new_price: float = Form(...),
    csrf_token: str = Form(...),
    admin_tg_id: int = Depends(require_admin),
    _csrf: None = Depends(csrf_check),
):
    if new_price <= 0 or new_price > 1_000_000:
        return _redirect_to_order(order_id, "err", "Цена должна быть положительной и не более 1 000 000 ₽")

    async with AsyncSessionLocal() as session:
        order = await get_order(session, order_id)
        if order is None:
            return _redirect_to_order(order_id, "err", f"Заказ #{order_id} не найден")
        if order.status in {OrderStatus.DONE.value, OrderStatus.REFUNDED.value,
                            OrderStatus.CANCELLED.value}:
            return _redirect_to_order(order_id, "err",
                                       f"Нельзя менять цену финального заказа ({order.status})")

        old_price = order.price
        await update_order_price(session, order_id, new_price)

        r = await session.execute(select(Order).options(selectinload(Order.user)).where(Order.id == order_id))
        order = r.scalar_one()
        student_tg_id = order.user.tg_id if order.user else None

    if student_tg_id:
        await notify_telegram(
            student_tg_id,
            f"💰 <b>Цена вашего заказа #{order_id} обновлена</b>\n\n"
            f"Новая цена: <b>{new_price:.0f} ₽</b>",
        )

    await log_admin_action(
        admin_tg_id=admin_tg_id, action="change_price",
        target_type="order", target_id=order_id,
        ip_address=get_client_ip(request),
        user_agent=request.headers.get("user-agent", ""),
        details={"old_price": old_price, "new_price": new_price},
    )

    return _redirect_to_order(order_id, "ok",
                               f"Цена изменена: {old_price or 0:.0f} → {new_price:.0f} ₽. Студент уведомлён.")


# ─── Send to editor ──────────────────────────────────────────────────────────

@router.post("/admin/orders/{order_id}/send-to-editor")
@limiter.limit("10/minute")
async def send_to_editor(
    request: Request,
    order_id: int,
    comment: str = Form(""),
    csrf_token: str = Form(...),
    files: List[UploadFile] = FastAPIFile(...),
    admin_tg_id: int = Depends(require_admin),
    _csrf: None = Depends(csrf_check),
):
    # ─── Валидация заказа ───
    async with AsyncSessionLocal() as session:
        order = await get_order(session, order_id)
        if order is None:
            return _redirect_to_order(order_id, "err", f"Заказ #{order_id} не найден")
        if order.status != OrderStatus.IN_WORK.value:
            return _redirect_to_order(order_id, "err",
                                       f"Отправить редактору можно только IN_WORK-заказ (сейчас {order.status})")

    # ─── Валидация файлов ───
    if not files or len(files) == 0:
        return _redirect_to_order(order_id, "err", "Загрузите хотя бы один файл")
    if len(files) > MAX_FILES:
        return _redirect_to_order(order_id, "err", f"Максимум {MAX_FILES} файлов за раз")

    # Проверка размеров и расширений + чтение
    file_data = []
    for f in files:
        ext = os.path.splitext(f.filename or "")[1].lower()
        if ext not in ALLOWED_EXTENSIONS:
            return _redirect_to_order(order_id, "err",
                                       f"Недопустимое расширение: {ext}. Разрешены: {', '.join(sorted(ALLOWED_EXTENSIONS))}")
        content = await f.read()
        if len(content) > MAX_FILE_SIZE:
            return _redirect_to_order(order_id, "err",
                                       f"Файл «{f.filename}» больше 10 МБ ({len(content) // 1024 // 1024} МБ)")
        if len(content) == 0:
            return _redirect_to_order(order_id, "err", f"Файл «{f.filename}» пустой")
        safe_name = _secure_filename(f.filename or "file")
        file_data.append((safe_name, content))

    # ─── Сохранение ───
    upload_dir = os.path.abspath(settings.UPLOADS_DIR)
    os.makedirs(upload_dir, exist_ok=True)

    saved_paths = []
    async with AsyncSessionLocal() as session:
        for i, (safe_name, content) in enumerate(file_data):
            local_path = os.path.join(upload_dir, f"order_{order_id}_draft_{i}_{safe_name}")
            try:
                with open(local_path, "wb") as out:
                    out.write(content)
            except OSError as e:
                logger.error(f"Не удалось сохранить файл {local_path}: {e}")
                return _redirect_to_order(order_id, "err", f"Не удалось сохранить файл «{safe_name}»")
            await save_file(
                session, order_id,
                FileType.DRAFT,
                filename=safe_name,
                local_path=local_path,
            )
            saved_paths.append((safe_name, local_path))

        # Меняем статус
        await update_order_status(session, order_id, OrderStatus.EDITING)

        # вытаскиваем студента для уведомления
        r = await session.execute(select(Order).options(selectinload(Order.user)).where(Order.id == order_id))
        order = r.scalar_one()
        student_tg_id = order.user.tg_id if order.user else None

    # ─── Уведомления ───
    comment_clean = (comment or "").strip()
    if not settings.EDITOR_TG_ID:
        logger.warning("EDITOR_TG_ID не задан — файлы сохранены, но редактору не отправлены")
        return _redirect_to_order(order_id, "err",
                                   "Файлы сохранены, но EDITOR_TG_ID не настроен. Свяжитесь с админом сервера.")

    # Шлём редактору каждый файл
    sent_count = 0
    for i, (safe_name, local_path) in enumerate(saved_paths):
        caption = None
        if i == 0:
            caption = (
                f"🛠 <b>ЗАДАНИЕ ДЛЯ РЕДАКТОРА</b>\n\n"
                f"Заказ #{order_id}\n\n"
                f"{('Комментарий:\n' + comment_clean) if comment_clean else 'Без комментария'}"
            )
        if await send_document_telegram(
            settings.EDITOR_TG_ID, local_path,
            caption=caption, filename=safe_name,
        ):
            sent_count += 1

    if student_tg_id:
        await notify_telegram(
            student_tg_id,
            f"📋 <b>Ваш заказ #{order_id} передан редактору</b>\n\n"
            f"Следите за обновлениями — скоро мы пришлём превью.",
        )

    await log_admin_action(
        admin_tg_id=admin_tg_id, action="send_to_editor",
        target_type="order", target_id=order_id,
        ip_address=get_client_ip(request),
        user_agent=request.headers.get("user-agent", ""),
        details={"file_count": len(saved_paths), "sent_count": sent_count,
                 "comment_len": len(comment_clean)},
    )

    msg = f"Отправлено редактору: {sent_count}/{len(saved_paths)} файлов. Заказ #{order_id} → EDITING."
    return _redirect_to_order(order_id, "ok" if sent_count == len(saved_paths) else "err", msg)
