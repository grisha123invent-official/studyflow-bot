"""
Дашборд редактора — KPI и срочные задания.
"""
import logging
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse
from sqlalchemy import select, func

from db.models import Order, OrderStatus
from db.session import AsyncSessionLocal
from web.anonymize import student_alias
from web.deps_editor import require_editor, EDITOR_CSRF_COOKIE_NAME
from web.security.csrf import generate_csrf_token
from web.templating import render_editor

logger = logging.getLogger("editor.dashboard")
router = APIRouter()


@router.get("/editor", response_class=HTMLResponse)
@router.get("/editor/", response_class=HTMLResponse)
async def editor_dashboard(request: Request, editor_tg_id: int = Depends(require_editor)):
    async with AsyncSessionLocal() as session:
        # Счётчики по статусам
        r = await session.execute(
            select(Order.status, func.count(Order.id))
            .where(Order.status.in_([
                OrderStatus.EDITING.value,
                OrderStatus.PREVIEW_SENT.value,
                OrderStatus.AWAITING_PAYMENT.value,
                OrderStatus.DONE.value,
            ]))
            .group_by(Order.status)
        )
        counts = {row[0]: row[1] for row in r.all()}

        # Срочные правки: EDITING + revision_comment IS NOT NULL
        r = await session.execute(
            select(func.count(Order.id))
            .where(Order.status == OrderStatus.EDITING.value,
                   Order.revision_comment.isnot(None))
        )
        revisions_pending = r.scalar_one() or 0

        # Готово сегодня
        today_start = datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0, tzinfo=None)
        r = await session.execute(
            select(func.count(Order.id))
            .where(Order.status == OrderStatus.DONE.value)
        )
        done_total = r.scalar_one() or 0

        # Список 5 ожидающих заданий (EDITING) + правки сверху
        r = await session.execute(
            select(Order)
            .where(Order.status == OrderStatus.EDITING.value)
            .order_by(Order.revision_comment.is_(None), Order.created_at.asc())
            .limit(5)
        )
        urgent = list(r.scalars().all())

    csrf = generate_csrf_token()
    response = render_editor(
        request, "editor/dashboard.html",
        {
            "title": "Дашборд",
            "subtitle": "Активные задания и срочные правки",
            "editor_tg_id": editor_tg_id,
            "csrf_token": csrf,
            "editing_count": counts.get(OrderStatus.EDITING.value, 0),
            "preview_sent_count": counts.get(OrderStatus.PREVIEW_SENT.value, 0),
            "awaiting_payment_count": counts.get(OrderStatus.AWAITING_PAYMENT.value, 0),
            "done_total": done_total,
            "revisions_pending": revisions_pending,
            "urgent_tasks": [
                {
                    "id": o.id,
                    "alias": student_alias(o.user_id),
                    "work_type": o.work_type,
                    "topic": o.topic,
                    "created_at": o.created_at,
                    "is_revision": bool(o.revision_comment),
                    "revisions_used": o.revisions_used,
                }
                for o in urgent
            ],
        },
    )
    response.set_cookie(
        key=EDITOR_CSRF_COOKIE_NAME, value=csrf,
        httponly=True, secure=True, samesite="strict",
        max_age=24 * 3600, path="/editor",
    )
    return response
