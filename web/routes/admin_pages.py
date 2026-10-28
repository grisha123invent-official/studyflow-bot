"""
Остальные страницы админ-панели (read-only пока):
  GET /admin/users         — список пользователей
  GET /admin/users/{tg_id} — карточка пользователя
  GET /admin/payments      — все платежи
  GET /admin/refunds       — заявки на возврат
  GET /admin/withdrawals   — заявки на вывод реф-баланса
  GET /admin/audit         — журнал действий администратора
  GET /admin/stats         — аналитика (с Chart.js)

Действия (одобрение/отклонение) — будут добавлены в следующих этапах.
"""
import logging
from typing import Optional

from fastapi import APIRouter, Depends, Request, Query
from fastapi.responses import HTMLResponse
from sqlalchemy import select, or_, func, desc
from sqlalchemy.orm import selectinload

from db.session import AsyncSessionLocal
from db.models import (
    User, Order, Payment, RefundRequest, WithdrawalRequest, AdminAuditLog,
    OrderStatus, PaymentStatus, RefundStatus, WithdrawalStatus, ReferralBonus,
)
from web.deps import require_admin
from web.security.csrf import generate_csrf_token, CSRF_COOKIE_NAME
from web.templating import templates, render

logger = logging.getLogger("admin.pages")
router = APIRouter()


def _set_csrf(response, csrf: str) -> None:
    response.set_cookie(
        key=CSRF_COOKIE_NAME, value=csrf,
        httponly=True, secure=True, samesite="strict",
        max_age=24 * 3600, path="/admin",
    )


# ─── Пользователи ────────────────────────────────────────────────────────────

@router.get("/admin/users", response_class=HTMLResponse)
async def users_list(
    request: Request,
    admin_tg_id: int = Depends(require_admin),
    q: Optional[str] = Query(None),
    role: Optional[str] = Query(None),
    page: int = Query(1, ge=1),
):
    per_page = 50
    offset = (page - 1) * per_page

    async with AsyncSessionLocal() as session:
        stmt = select(User).order_by(User.created_at.desc())
        count_stmt = select(func.count(User.id))

        if role and role in ("student", "admin", "editor"):
            stmt = stmt.where(User.role == role)
            count_stmt = count_stmt.where(User.role == role)

        if q and q.strip():
            like = f"%{q.strip()}%"
            conds = [User.username.ilike(like), User.full_name.ilike(like), User.email.ilike(like)]
            if q.strip().isdigit():
                conds.append(User.tg_id == int(q.strip()))
            stmt = stmt.where(or_(*conds))
            count_stmt = count_stmt.where(or_(*conds))

        total = (await session.execute(count_stmt)).scalar() or 0
        users = list((await session.execute(stmt.limit(per_page).offset(offset))).scalars().all())

        # счётчик заказов и платежей по каждому юзеру
        user_ids = [u.id for u in users]
        orders_count = {}
        if user_ids:
            r = await session.execute(
                select(Order.user_id, func.count(Order.id))
                .where(Order.user_id.in_(user_ids))
                .group_by(Order.user_id)
            )
            orders_count = {row[0]: row[1] for row in r.all()}

    total_pages = max(1, (total + per_page - 1) // per_page)

    csrf = generate_csrf_token()
    response = render(
        request, "users/list.html",
        {
            "title": "Пользователи",
            "subtitle": f"Найдено: {total}",
            "admin_tg_id": admin_tg_id,
            "csrf_token": csrf,
            "users": users,
            "q": q or "",
            "role": role,
            "page": page,
            "total_pages": total_pages,
            "orders_count": orders_count,
        },
    )
    _set_csrf(response, csrf)
    return response


@router.get("/admin/users/{tg_id}", response_class=HTMLResponse)
async def user_detail(
    request: Request,
    tg_id: int,
    admin_tg_id: int = Depends(require_admin),
):
    async with AsyncSessionLocal() as session:
        r = await session.execute(select(User).where(User.tg_id == tg_id))
        user = r.scalar_one_or_none()

        if user is None:
            csrf = generate_csrf_token()
            response = render(
                request, "users/not_found.html",
                {"title": "Пользователь не найден", "admin_tg_id": admin_tg_id,
                 "csrf_token": csrf, "tg_id": tg_id},
                status_code=404,
            )
            _set_csrf(response, csrf)
            return response

        # Заказы пользователя
        r = await session.execute(
            select(Order).where(Order.user_id == user.id)
            .order_by(Order.created_at.desc()).limit(50)
        )
        user_orders = list(r.scalars().all())

        # Реф-бонусы
        r = await session.execute(
            select(ReferralBonus).where(ReferralBonus.referrer_id == user.id)
            .order_by(ReferralBonus.created_at.desc()).limit(20)
        )
        bonuses = list(r.scalars().all())

        # Кол-во приглашённых
        r = await session.execute(
            select(func.count(User.id)).where(User.referred_by_id == user.id)
        )
        referred_count = r.scalar_one() or 0

    csrf = generate_csrf_token()
    response = render(
        request, "users/detail.html",
        {
            "title": user.full_name or (("@" + user.username) if user.username else f"id{user.tg_id}"),
            "subtitle": f"tg_id: {user.tg_id} · роль: {user.role}",
            "admin_tg_id": admin_tg_id,
            "csrf_token": csrf,
            "user": user,
            "user_orders": user_orders,
            "bonuses": bonuses,
            "referred_count": referred_count,
        },
    )
    _set_csrf(response, csrf)
    return response


# ─── Платежи ─────────────────────────────────────────────────────────────────

@router.get("/admin/payments", response_class=HTMLResponse)
async def payments_list(
    request: Request,
    admin_tg_id: int = Depends(require_admin),
    status_f: Optional[str] = Query(None, alias="status"),
    page: int = Query(1, ge=1),
):
    per_page = 50
    offset = (page - 1) * per_page

    async with AsyncSessionLocal() as session:
        stmt = (
            select(Payment)
            .options(selectinload(Payment.order).selectinload(Order.user))
            .order_by(Payment.created_at.desc())
        )
        count_stmt = select(func.count(Payment.id))

        if status_f and status_f in {s.value for s in PaymentStatus}:
            stmt = stmt.where(Payment.status == status_f)
            count_stmt = count_stmt.where(Payment.status == status_f)

        total = (await session.execute(count_stmt)).scalar() or 0
        payments = list((await session.execute(stmt.limit(per_page).offset(offset))).scalars().all())

        # Сумма по PAID
        r = await session.execute(
            select(func.coalesce(func.sum(Payment.amount), 0.0))
            .where(Payment.status == PaymentStatus.PAID.value)
        )
        total_paid = float(r.scalar_one() or 0.0)

    total_pages = max(1, (total + per_page - 1) // per_page)

    csrf = generate_csrf_token()
    response = render(
        request, "payments/list.html",
        {
            "title": "Платежи",
            "subtitle": f"Всего: {total} · Оплачено: {total_paid:.0f} ₽",
            "admin_tg_id": admin_tg_id,
            "csrf_token": csrf,
            "payments": payments,
            "status": status_f,
            "page": page,
            "total_pages": total_pages,
            "total_paid": total_paid,
        },
    )
    _set_csrf(response, csrf)
    return response


# ─── Возвраты ────────────────────────────────────────────────────────────────

@router.get("/admin/refunds", response_class=HTMLResponse)
async def refunds_list(
    request: Request,
    admin_tg_id: int = Depends(require_admin),
    status_f: Optional[str] = Query(None, alias="status"),
):
    async with AsyncSessionLocal() as session:
        stmt = (
            select(RefundRequest)
            .options(
                selectinload(RefundRequest.user),
                selectinload(RefundRequest.order),
            )
            .order_by(RefundRequest.created_at.desc())
        )
        if status_f and status_f in {s.value for s in RefundStatus}:
            stmt = stmt.where(RefundRequest.status == status_f)
        refunds = list((await session.execute(stmt.limit(200))).scalars().all())

        # счётчики по статусам
        r = await session.execute(
            select(RefundRequest.status, func.count(RefundRequest.id))
            .group_by(RefundRequest.status)
        )
        counts = {row[0]: row[1] for row in r.all()}

    csrf = generate_csrf_token()
    response = render(
        request, "refunds/list.html",
        {
            "title": "Возвраты",
            "subtitle": f"Ожидают решения: {counts.get('pending', 0)}",
            "admin_tg_id": admin_tg_id,
            "csrf_token": csrf,
            "refunds": refunds,
            "status": status_f,
            "counts": counts,
        },
    )
    _set_csrf(response, csrf)
    return response


# ─── Выводы реф-баланса ──────────────────────────────────────────────────────

@router.get("/admin/withdrawals", response_class=HTMLResponse)
async def withdrawals_list(
    request: Request,
    admin_tg_id: int = Depends(require_admin),
    status_f: Optional[str] = Query(None, alias="status"),
):
    async with AsyncSessionLocal() as session:
        stmt = (
            select(WithdrawalRequest)
            .options(selectinload(WithdrawalRequest.user))
            .order_by(WithdrawalRequest.created_at.desc())
        )
        if status_f and status_f in {s.value for s in WithdrawalStatus}:
            stmt = stmt.where(WithdrawalRequest.status == status_f)
        withdrawals = list((await session.execute(stmt.limit(200))).scalars().all())

        r = await session.execute(
            select(WithdrawalRequest.status, func.count(WithdrawalRequest.id))
            .group_by(WithdrawalRequest.status)
        )
        counts = {row[0]: row[1] for row in r.all()}

        # сумма pending
        r = await session.execute(
            select(func.coalesce(func.sum(WithdrawalRequest.amount), 0.0))
            .where(WithdrawalRequest.status == WithdrawalStatus.PENDING.value)
        )
        pending_sum = float(r.scalar_one() or 0.0)

    csrf = generate_csrf_token()
    response = render(
        request, "withdrawals/list.html",
        {
            "title": "Выводы реф-баланса",
            "subtitle": f"Ожидают: {counts.get('pending', 0)} · Сумма: {pending_sum:.0f} ₽",
            "admin_tg_id": admin_tg_id,
            "csrf_token": csrf,
            "withdrawals": withdrawals,
            "status": status_f,
            "counts": counts,
            "pending_sum": pending_sum,
        },
    )
    _set_csrf(response, csrf)
    return response


# ─── Журнал действий ─────────────────────────────────────────────────────────

@router.get("/admin/audit", response_class=HTMLResponse)
async def audit_log(
    request: Request,
    admin_tg_id: int = Depends(require_admin),
    action_f: Optional[str] = Query(None, alias="action"),
    page: int = Query(1, ge=1),
):
    per_page = 100
    offset = (page - 1) * per_page

    async with AsyncSessionLocal() as session:
        stmt = select(AdminAuditLog).order_by(desc(AdminAuditLog.created_at))
        count_stmt = select(func.count(AdminAuditLog.id))

        if action_f:
            stmt = stmt.where(AdminAuditLog.action == action_f)
            count_stmt = count_stmt.where(AdminAuditLog.action == action_f)

        total = (await session.execute(count_stmt)).scalar() or 0
        logs = list((await session.execute(stmt.limit(per_page).offset(offset))).scalars().all())

        # Уникальные actions для фильтра
        r = await session.execute(
            select(AdminAuditLog.action, func.count(AdminAuditLog.id))
            .group_by(AdminAuditLog.action)
            .order_by(desc(func.count(AdminAuditLog.id)))
        )
        action_counts = [(row[0], row[1]) for row in r.all()]

    total_pages = max(1, (total + per_page - 1) // per_page)

    csrf = generate_csrf_token()
    response = render(
        request, "audit/list.html",
        {
            "title": "Журнал действий",
            "subtitle": f"Записей: {total}",
            "admin_tg_id": admin_tg_id,
            "csrf_token": csrf,
            "logs": logs,
            "action": action_f,
            "action_counts": action_counts,
            "page": page,
            "total_pages": total_pages,
        },
    )
    _set_csrf(response, csrf)
    return response


# ─── Аналитика ───────────────────────────────────────────────────────────────

@router.get("/admin/stats", response_class=HTMLResponse)
async def stats_page(
    request: Request,
    admin_tg_id: int = Depends(require_admin),
):
    from datetime import datetime, timedelta, timezone

    async with AsyncSessionLocal() as session:
        # Выручка по дням за последние 30 дней ВКЛЮЧАЯ сегодня.
        # start = сегодня 00:00 минус 29 дней → диапазон [start, today] = 30 дней.
        now = datetime.now(timezone.utc).replace(tzinfo=None)
        today_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
        start = today_start - timedelta(days=29)

        r = await session.execute(
            select(
                func.strftime("%Y-%m-%d", Payment.paid_at).label("d"),
                func.coalesce(func.sum(Payment.amount), 0.0),
            )
            .where(Payment.status == PaymentStatus.PAID.value, Payment.paid_at >= start)
            .group_by("d")
            .order_by("d")
        )
        revenue_by_day = [(row[0], float(row[1])) for row in r.all()]

        # Заполнить пропущенные дни нулями
        rev_map = dict(revenue_by_day)
        days = []
        amounts = []
        for i in range(30):
            d = (start + timedelta(days=i)).strftime("%Y-%m-%d")
            days.append(d)
            amounts.append(rev_map.get(d, 0.0))

        # Заказы по статусам
        r = await session.execute(
            select(Order.status, func.count(Order.id)).group_by(Order.status)
        )
        STATUS_RU = {
            "FILLING": "Заполнение",
            "PENDING": "Ожидают",
            "IN_WORK": "В работе",
            "EDITING": "У редактора",
            "PREVIEW_SENT": "Превью отправлено",
            "AWAITING_PAYMENT": "Ждут оплаты",
            "DONE": "Выполнены",
            "CANCELLED": "Отменены",
            "REFUNDED": "Возвращены",
        }
        orders_by_status = [(STATUS_RU.get(row[0], row[0]), row[1]) for row in r.all()]

        # Топ-10 рефереров
        r = await session.execute(
            select(User.tg_id, User.username, User.full_name, User.ref_balance)
            .where(User.ref_balance > 0)
            .order_by(desc(User.ref_balance))
            .limit(10)
        )
        top_referrers = [
            {"tg_id": row[0], "username": row[1], "full_name": row[2], "balance": float(row[3])}
            for row in r.all()
        ]

        # Общая статистика
        r = await session.execute(select(func.count(User.id)))
        total_users = r.scalar_one() or 0
        r = await session.execute(select(func.count(Order.id)))
        total_orders = r.scalar_one() or 0
        r = await session.execute(
            select(func.coalesce(func.sum(Payment.amount), 0.0))
            .where(Payment.status == PaymentStatus.PAID.value)
        )
        total_revenue = float(r.scalar_one() or 0.0)
        r = await session.execute(
            select(func.count(Order.id)).where(Order.status == OrderStatus.DONE.value)
        )
        orders_done = r.scalar_one() or 0
        conversion = (orders_done / total_orders * 100) if total_orders else 0.0

    csrf = generate_csrf_token()
    response = render(
        request, "stats/index.html",
        {
            "title": "Аналитика",
            "subtitle": "Графики за последние 30 дней",
            "admin_tg_id": admin_tg_id,
            "csrf_token": csrf,
            "days": days,
            "amounts": amounts,
            "orders_by_status": orders_by_status,
            "top_referrers": top_referrers,
            "total_users": total_users,
            "total_orders": total_orders,
            "total_revenue": total_revenue,
            "orders_done": orders_done,
            "conversion": conversion,
        },
    )
    _set_csrf(response, csrf)
    return response
