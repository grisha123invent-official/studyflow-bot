"""
Список заданий редактора + карточка задания.
"""
import logging
from typing import Optional

from fastapi import APIRouter, Depends, Request, Query
from fastapi.responses import HTMLResponse
from sqlalchemy import select, func, or_
from sqlalchemy.orm import selectinload

from db.models import Order, OrderStatus, FileType, File as FileModel
from db.session import AsyncSessionLocal
from web.anonymize import student_alias
from web.deps_editor import require_editor, EDITOR_CSRF_COOKIE_NAME
from web.security.csrf import generate_csrf_token
from web.templating import render_editor

logger = logging.getLogger("editor.tasks")
router = APIRouter()


# Статусы, видимые редактору
EDITOR_STATUSES = [
    OrderStatus.EDITING.value,
    OrderStatus.PREVIEW_SENT.value,
    OrderStatus.AWAITING_PAYMENT.value,
    OrderStatus.DONE.value,
]


STATUS_META = {
    OrderStatus.EDITING.value:          {"label": "В работе",       "color": "amber"},
    OrderStatus.PREVIEW_SENT.value:     {"label": "Превью у клиента", "color": "sky"},
    OrderStatus.AWAITING_PAYMENT.value: {"label": "Ждёт оплаты",    "color": "orange"},
    OrderStatus.DONE.value:             {"label": "Выполнено",      "color": "emerald"},
}


def _serialize_task_card(o: Order) -> dict:
    """Анонимизированная сборка карточки задания — НЕ светим PII."""
    return {
        "id": o.id,
        "alias": student_alias(o.user_id),
        "work_type": o.work_type,
        "topic": o.topic,
        "subject": o.subject,
        "pages": o.pages,
        "deadline": o.deadline,
        "comment": o.comment,
        "status": o.status,
        "revision_comment": o.revision_comment,
        "revisions_used": o.revisions_used,
        "created_at": o.created_at,
        "is_revision": bool(o.revision_comment),
    }


# ─── Список ──────────────────────────────────────────────────────────────────

@router.get("/editor/tasks", response_class=HTMLResponse)
async def tasks_list(
    request: Request,
    editor_tg_id: int = Depends(require_editor),
    status: Optional[str] = Query(None),
):
    # По умолчанию — EDITING
    target_status = None
    if status:
        s_upper = status.upper()
        if s_upper in EDITOR_STATUSES:
            target_status = s_upper

    async with AsyncSessionLocal() as session:
        stmt = (
            select(Order)
            .where(Order.status.in_(EDITOR_STATUSES))
            .order_by(Order.revision_comment.is_(None), Order.created_at.asc())
        )
        if target_status:
            stmt = select(Order).where(Order.status == target_status).order_by(
                Order.revision_comment.is_(None), Order.created_at.asc()
            )

        tasks = list((await session.execute(stmt.limit(100))).scalars().all())

        # Счётчики по статусам
        r = await session.execute(
            select(Order.status, func.count(Order.id))
            .where(Order.status.in_(EDITOR_STATUSES))
            .group_by(Order.status)
        )
        counts = {row[0]: row[1] for row in r.all()}

    csrf = generate_csrf_token()
    response = render_editor(
        request, "editor/tasks/list.html",
        {
            "title": "Задания",
            "subtitle": f"Найдено: {len(tasks)}",
            "editor_tg_id": editor_tg_id,
            "csrf_token": csrf,
            "tasks": [_serialize_task_card(t) for t in tasks],
            "status": target_status,
            "counts": counts,
            "status_meta": STATUS_META,
            "editor_statuses": EDITOR_STATUSES,
        },
    )
    response.set_cookie(
        key=EDITOR_CSRF_COOKIE_NAME, value=csrf,
        httponly=True, secure=True, samesite="strict",
        max_age=24 * 3600, path="/editor",
    )
    return response


# ─── Карточка ────────────────────────────────────────────────────────────────

@router.get("/editor/tasks/{order_id}", response_class=HTMLResponse)
async def task_detail(
    request: Request,
    order_id: int,
    editor_tg_id: int = Depends(require_editor),
):
    async with AsyncSessionLocal() as session:
        r = await session.execute(
            select(Order)
            .options(selectinload(Order.files))
            .where(Order.id == order_id)
        )
        order = r.scalar_one_or_none()

        # Был ли заказ оплачен — чтобы отличить pre-payment правку от post-payment.
        # pre-payment правка: пересоздаём превью с watermark → отправляем как обычное превью.
        # post-payment правка: отправляем финал напрямую без watermark (через submit-revision).
        from db.models import Payment, PaymentStatus
        is_paid = False
        if order is not None:
            pr = await session.execute(
                select(Payment.id).where(
                    Payment.order_id == order_id,
                    Payment.status == PaymentStatus.PAID.value,
                ).limit(1)
            )
            is_paid = pr.scalar_one_or_none() is not None

    if order is None or order.status not in EDITOR_STATUSES:
        # Не существует или не доступно редактору
        csrf = generate_csrf_token()
        response = render_editor(
            request, "editor/tasks/not_found.html",
            {
                "title": "Задание недоступно",
                "editor_tg_id": editor_tg_id,
                "csrf_token": csrf,
                "order_id": order_id,
            },
            status_code=404,
        )
        return response

    # Группируем файлы по типу — НЕ светим owner-данные
    files_by_type = {
        FileType.STUDENT_TEMPLATE.value: [],
        FileType.DRAFT.value: [],
        FileType.PREVIEW.value: [],
        FileType.FINAL.value: [],
    }
    for f in order.files:
        ftype = f.file_type if isinstance(f.file_type, str) else f.file_type.value
        if ftype in files_by_type:
            files_by_type[ftype].append({
                "id": f.id,
                "filename": f.filename or "без имени",
                "uploaded_at": f.uploaded_at,
                "is_watermarked": f.is_watermarked,
            })
    # Сортируем по дате (новые сверху)
    for k in files_by_type:
        files_by_type[k].sort(key=lambda x: x["uploaded_at"] or "", reverse=True)

    # Последнее превью (для inline-просмотра)
    latest_preview = files_by_type[FileType.PREVIEW.value][0] if files_by_type[FileType.PREVIEW.value] else None
    latest_final = files_by_type[FileType.FINAL.value][0] if files_by_type[FileType.FINAL.value] else None

    csrf = generate_csrf_token()
    response = render_editor(
        request, "editor/tasks/detail.html",
        {
            "title": f"Задание #{order.id}",
            "subtitle": student_alias(order.user_id),
            "editor_tg_id": editor_tg_id,
            "csrf_token": csrf,
            "task": _serialize_task_card(order),
            "files_by_type": files_by_type,
            "latest_preview": latest_preview,
            "latest_final": latest_final,
            "status_meta": STATUS_META,
            "is_paid": is_paid,
        },
    )
    response.set_cookie(
        key=EDITOR_CSRF_COOKIE_NAME, value=csrf,
        httponly=True, secure=True, samesite="strict",
        max_age=24 * 3600, path="/editor",
    )
    return response
