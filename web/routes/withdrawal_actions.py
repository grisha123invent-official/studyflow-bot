"""
Действия по заявкам на вывод реф-баланса.

POST /admin/withdrawals/{id}/approve — одобрить
POST /admin/withdrawals/{id}/reject  — отклонить (баланс возвращается)
"""
import logging

from fastapi import APIRouter, Depends, Request, Form
from fastapi.responses import RedirectResponse
from sqlalchemy import select
from sqlalchemy.orm import selectinload

from db.crud import update_withdrawal_status
from db.models import WithdrawalRequest, WithdrawalStatus
from db.session import AsyncSessionLocal
from web.audit import log_admin_action
from web.deps import require_admin, csrf_check
from web.flash import set_flash
from web.notifications import notify_telegram
from web.security.rate_limit import limiter, get_client_ip

logger = logging.getLogger("admin.withdrawal_actions")
router = APIRouter()


def _redirect(level: str, message: str) -> RedirectResponse:
    response = RedirectResponse("/admin/withdrawals", status_code=303)
    set_flash(response, level, message)
    return response


@router.post("/admin/withdrawals/{request_id}/approve")
@limiter.limit("30/minute")
async def approve_withdrawal(
    request: Request,
    request_id: int,
    csrf_token: str = Form(...),
    admin_tg_id: int = Depends(require_admin),
    _csrf: None = Depends(csrf_check),
):
    async with AsyncSessionLocal() as session:
        r = await session.execute(
            select(WithdrawalRequest)
            .options(selectinload(WithdrawalRequest.user))
            .where(WithdrawalRequest.id == request_id)
        )
        req = r.scalar_one_or_none()
        if req is None:
            return _redirect("err", f"Заявка #{request_id} не найдена")

        current = req.status if isinstance(req.status, str) else req.status.value
        if current != WithdrawalStatus.PENDING.value:
            return _redirect("err", f"Заявка #{request_id} уже обработана ({current})")

        student_tg_id = req.user.tg_id if req.user else None
        amount = req.amount
        card_details = req.card_details

        await update_withdrawal_status(session, request_id, WithdrawalStatus.APPROVED)

    if student_tg_id:
        await notify_telegram(
            student_tg_id,
            f"✅ <b>Ваш запрос на вывод одобрен</b>\n\n"
            f"Сумма: <b>{amount:.0f} ₽</b>\n"
            f"Реквизиты: <code>{card_details}</code>\n\n"
            f"Ожидайте перевода.",
        )

    await log_admin_action(
        admin_tg_id=admin_tg_id, action="approve_withdrawal",
        target_type="withdrawal", target_id=request_id,
        ip_address=get_client_ip(request),
        user_agent=request.headers.get("user-agent", ""),
        details={"amount": amount},
    )

    return _redirect("ok", f"Вывод #{request_id} одобрен. Не забудьте сделать перевод вручную.")


@router.post("/admin/withdrawals/{request_id}/reject")
@limiter.limit("30/minute")
async def reject_withdrawal(
    request: Request,
    request_id: int,
    reason: str = Form(...),
    csrf_token: str = Form(...),
    admin_tg_id: int = Depends(require_admin),
    _csrf: None = Depends(csrf_check),
):
    reason_clean = (reason or "").strip()
    if len(reason_clean) < 3:
        return _redirect("err", "Причина отказа должна быть минимум 3 символа")

    async with AsyncSessionLocal() as session:
        r = await session.execute(
            select(WithdrawalRequest)
            .options(selectinload(WithdrawalRequest.user))
            .where(WithdrawalRequest.id == request_id)
        )
        req = r.scalar_one_or_none()
        if req is None:
            return _redirect("err", f"Заявка #{request_id} не найдена")

        current = req.status if isinstance(req.status, str) else req.status.value
        if current != WithdrawalStatus.PENDING.value:
            return _redirect("err", f"Заявка #{request_id} уже обработана")

        student_tg_id = req.user.tg_id if req.user else None
        amount = req.amount

        # update_withdrawal_status сам вернёт сумму на баланс при REJECTED
        await update_withdrawal_status(session, request_id, WithdrawalStatus.REJECTED)

    if student_tg_id:
        await notify_telegram(
            student_tg_id,
            f"❌ <b>Ваш запрос на вывод отклонён</b>\n\n"
            f"Сумма: {amount:.0f} ₽\n"
            f"Причина: {reason_clean}\n\n"
            f"Средства возвращены на ваш реф-баланс.",
        )

    await log_admin_action(
        admin_tg_id=admin_tg_id, action="reject_withdrawal",
        target_type="withdrawal", target_id=request_id,
        ip_address=get_client_ip(request),
        user_agent=request.headers.get("user-agent", ""),
        details={"reason": reason_clean, "amount": amount},
    )

    return _redirect("ok", f"Вывод #{request_id} отклонён. Баланс возвращён юзеру.")
