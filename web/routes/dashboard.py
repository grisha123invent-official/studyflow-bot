"""
Дашборд / стартовая страница админ-панели.
"""
import logging
from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse

from db.crud import get_admin_analytics, count_pending_refund_requests, count_pending_withdrawals
from db.session import AsyncSessionLocal
from db.models import OrderStatus
from web.deps import require_admin
from web.security.csrf import generate_csrf_token, CSRF_COOKIE_NAME
from web.templating import templates

logger = logging.getLogger("admin.dashboard")
router = APIRouter()


@router.get("/admin", response_class=HTMLResponse)
@router.get("/admin/", response_class=HTMLResponse)
async def dashboard(request: Request, admin_tg_id: int = Depends(require_admin)):
    async with AsyncSessionLocal() as session:
        stats = await get_admin_analytics(session)
        pending_refunds = await count_pending_refund_requests(session)
        pending_withdrawals = await count_pending_withdrawals(session)

    os_ = stats["orders_by_status"]
    csrf = generate_csrf_token()
    response = templates.TemplateResponse(
        request, "dashboard.html",
        {
            "title": "Дашборд",
            "subtitle": "Сводка по всему сервису",
            "admin_tg_id": admin_tg_id,
            "csrf_token": csrf,
            "stats": stats,
            "os": OrderStatus,
            "orders_pending": os_.get(OrderStatus.PENDING.value, 0),
            "orders_in_work": os_.get(OrderStatus.IN_WORK.value, 0),
            "orders_editing": os_.get(OrderStatus.EDITING.value, 0),
            "orders_preview": os_.get(OrderStatus.PREVIEW_SENT.value, 0),
            "orders_awaiting": os_.get(OrderStatus.AWAITING_PAYMENT.value, 0),
            "orders_done": os_.get(OrderStatus.DONE.value, 0),
            "orders_cancelled": os_.get(OrderStatus.CANCELLED.value, 0),
            "orders_refunded": os_.get(OrderStatus.REFUNDED.value, 0),
            "pending_refunds": pending_refunds,
            "pending_withdrawals": pending_withdrawals,
        },
    )
    response.set_cookie(
        key=CSRF_COOKIE_NAME, value=csrf,
        httponly=True, secure=True, samesite="strict",
        max_age=24 * 3600, path="/admin",
    )
    return response
