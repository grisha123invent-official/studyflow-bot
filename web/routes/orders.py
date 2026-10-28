"""
Заказы — список с фильтрами и карточка заказа.

Маршруты:
  GET /admin/orders            — таблица всех заказов с фильтром по статусу/поиску
  GET /admin/orders/{order_id} — детальная карточка заказа
"""
import logging
from typing import Optional

from fastapi import APIRouter, Depends, Request, Query
from fastapi.responses import HTMLResponse
from sqlalchemy import select, or_, func
from sqlalchemy.orm import selectinload

from db.session import AsyncSessionLocal
from db.models import Order, User, Payment, File, OrderStatus
from web.deps import require_admin
from web.security.csrf import generate_csrf_token, CSRF_COOKIE_NAME
from web.templating import templates, render

logger = logging.getLogger("admin.orders")
router = APIRouter()


# ─── Метаданные статусов для UI ──────────────────────────────────────────────

STATUS_META = {
    "PENDING":          {"label": "Ожидают",     "emoji": "🟡", "icon": None,            "color": "amber"},
    "IN_WORK":          {"label": "В работе",    "emoji": "🟠", "icon": None,            "color": "orange"},
    "EDITING":          {"label": "У редактора", "emoji": "🔴", "icon": None,            "color": "rose"},
    "PREVIEW_SENT":     {"label": "Превью",      "emoji": "🔵", "icon": None,            "color": "sky"},
    "AWAITING_PAYMENT": {"label": "Ждут оплаты", "emoji": None, "icon": "credit-card",   "color": "amber"},
    "DONE":             {"label": "Выполнены",   "emoji": None, "icon": "check-circle-2","color": "emerald"},
    "CANCELLED":        {"label": "Отменены",    "emoji": None, "icon": "x-circle",      "color": "slate"},
    "REFUNDED":         {"label": "Возвращены",  "emoji": None, "icon": "undo-2",        "color": "rose"},
    "FILLING":          {"label": "Заполнение",  "emoji": None, "icon": "edit-3",        "color": "slate"},
}


# ─── Список заказов ──────────────────────────────────────────────────────────

@router.get("/admin/orders", response_class=HTMLResponse)
async def orders_list(
    request: Request,
    admin_tg_id: int = Depends(require_admin),
    status: Optional[str] = Query(None),
    q: Optional[str] = Query(None),
    page: int = Query(1, ge=1),
):
    per_page = 50
    offset = (page - 1) * per_page

    async with AsyncSessionLocal() as session:
        # Базовый запрос с join-load пользователя
        stmt = select(Order).options(selectinload(Order.user)).order_by(Order.created_at.desc())
        count_stmt = select(func.count(Order.id))

        # Фильтр по статусу
        if status:
            status_upper = status.upper()
            valid_statuses = {s.value for s in OrderStatus}
            if status_upper in valid_statuses:
                stmt = stmt.where(Order.status == status_upper)
                count_stmt = count_stmt.where(Order.status == status_upper)

        # Поиск по теме / order_id / username
        if q:
            q_clean = q.strip()
            if q_clean:
                # join к users только если ищем
                stmt = stmt.join(User, User.id == Order.user_id)
                count_stmt = count_stmt.select_from(Order).join(User, User.id == Order.user_id)
                like = f"%{q_clean}%"
                conds = [
                    Order.topic.ilike(like),
                    Order.subject.ilike(like),
                    User.username.ilike(like),
                    User.full_name.ilike(like),
                ]
                if q_clean.isdigit():
                    conds.append(Order.id == int(q_clean))
                    conds.append(User.tg_id == int(q_clean))
                stmt = stmt.where(or_(*conds))
                count_stmt = count_stmt.where(or_(*conds))

        total = (await session.execute(count_stmt)).scalar() or 0
        result = await session.execute(stmt.limit(per_page).offset(offset))
        orders = list(result.scalars().all())

        # Считаем заказы по каждому статусу для табов
        status_counts_stmt = select(Order.status, func.count(Order.id)).group_by(Order.status)
        status_counts_raw = (await session.execute(status_counts_stmt)).all()
        status_counts = {row[0]: row[1] for row in status_counts_raw}

    total_pages = max(1, (total + per_page - 1) // per_page)

    csrf = generate_csrf_token()
    response = templates.TemplateResponse(
        request, "orders/list.html",
        {
            "title": "Заказы",
            "subtitle": f"Найдено: {total}",
            "admin_tg_id": admin_tg_id,
            "csrf_token": csrf,
            "orders": orders,
            "status": status,
            "q": q or "",
            "page": page,
            "total_pages": total_pages,
            "total": total,
            "status_counts": status_counts,
            "status_meta": STATUS_META,
        },
    )
    response.set_cookie(
        key=CSRF_COOKIE_NAME, value=csrf,
        httponly=True, secure=True, samesite="strict",
        max_age=24 * 3600, path="/admin",
    )
    return response


# ─── Карточка заказа ─────────────────────────────────────────────────────────

@router.get("/admin/orders/{order_id}", response_class=HTMLResponse)
async def order_detail(
    request: Request,
    order_id: int,
    admin_tg_id: int = Depends(require_admin),
):
    async with AsyncSessionLocal() as session:
        result = await session.execute(
            select(Order)
            .options(
                selectinload(Order.user),
                selectinload(Order.files),
                selectinload(Order.payments),
            )
            .where(Order.id == order_id)
        )
        order = result.scalar_one_or_none()

    if order is None:
        csrf = generate_csrf_token()
        response = templates.TemplateResponse(
            request, "orders/not_found.html",
            {
                "title": "Заказ не найден",
                "admin_tg_id": admin_tg_id,
                "csrf_token": csrf,
                "order_id": order_id,
            },
            status_code=404,
        )
        return response

    csrf = generate_csrf_token()
    response = render(
        request, "orders/detail.html",
        {
            "title": f"Заказ #{order.id}",
            "subtitle": order.topic or "Без темы",
            "admin_tg_id": admin_tg_id,
            "csrf_token": csrf,
            "order": order,
            "status_meta": STATUS_META,
        },
    )
    response.set_cookie(
        key=CSRF_COOKIE_NAME, value=csrf,
        httponly=True, secure=True, samesite="strict",
        max_age=24 * 3600, path="/admin",
    )
    return response
