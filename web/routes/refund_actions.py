"""
Действия по заявкам на возврат.

POST /admin/refunds/{id}/approve — одобрить (заказ → REFUNDED). ⚠ возврат в Robokassa делается ВРУЧНУЮ в кабинете партнёра.
POST /admin/refunds/{id}/reject  — отклонить с причиной.
"""
import logging

from fastapi import APIRouter, Depends, Request, Form
from fastapi.responses import RedirectResponse
from sqlalchemy import select
from sqlalchemy.orm import selectinload

from db.crud import update_refund_status
from db.models import RefundRequest, RefundStatus
from db.session import AsyncSessionLocal
from web.audit import log_admin_action
from web.deps import require_admin, csrf_check
from web.flash import set_flash
from web.notifications import notify_telegram
from web.security.rate_limit import limiter, get_client_ip

logger = logging.getLogger("admin.refund_actions")
router = APIRouter()


def _redirect_to_refunds(level: str, message: str) -> RedirectResponse:
    response = RedirectResponse("/admin/refunds", status_code=303)
    set_flash(response, level, message)
    return response


@router.post("/admin/refunds/{refund_id}/approve")
@limiter.limit("30/minute")
async def approve_refund(
    request: Request,
    refund_id: int,
    csrf_token: str = Form(...),
    admin_tg_id: int = Depends(require_admin),
    _csrf: None = Depends(csrf_check),
):
    async with AsyncSessionLocal() as session:
        # Сначала получаем заявку с user/order для уведомления
        r = await session.execute(
            select(RefundRequest)
            .options(selectinload(RefundRequest.user), selectinload(RefundRequest.order))
            .where(RefundRequest.id == refund_id)
        )
        req = r.scalar_one_or_none()
        if req is None:
            return _redirect_to_refunds("err", f"Заявка #{refund_id} не найдена")

        current_status = req.status if isinstance(req.status, str) else req.status.value
        if current_status != RefundStatus.PENDING.value:
            return _redirect_to_refunds("err",
                                         f"Заявка #{refund_id} уже обработана (статус: {current_status})")

        student_tg_id = req.user.tg_id if req.user else None
        order_id = req.order_id

        # update_refund_status сам переводит заказ в REFUNDED при APPROVED
        await update_refund_status(session, refund_id, RefundStatus.APPROVED)

    if student_tg_id:
        await notify_telegram(
            student_tg_id,
            f"💰 <b>Ваш возврат одобрен</b>\n\n"
            f"По заказу #{order_id}. Средства поступят на карту в течение 1-10 рабочих дней.",
        )

    await log_admin_action(
        admin_tg_id=admin_tg_id, action="approve_refund",
        target_type="refund", target_id=refund_id,
        ip_address=get_client_ip(request),
        user_agent=request.headers.get("user-agent", ""),
        details={"order_id": order_id},
    )

    return _redirect_to_refunds(
        "ok",
        f"Возврат #{refund_id} одобрен. ⚠ Сделайте возврат вручную в кабинете Robokassa "
        f"(https://partner.robokassa.ru/) — у Robokassa нет API для авто-возврата."
    )


@router.post("/admin/refunds/{refund_id}/reject")
@limiter.limit("30/minute")
async def reject_refund(
    request: Request,
    refund_id: int,
    reason: str = Form(...),
    csrf_token: str = Form(...),
    admin_tg_id: int = Depends(require_admin),
    _csrf: None = Depends(csrf_check),
):
    reason_clean = (reason or "").strip()
    if len(reason_clean) < 5:
        return _redirect_to_refunds("err", "Причина отказа должна быть минимум 5 символов")

    async with AsyncSessionLocal() as session:
        r = await session.execute(
            select(RefundRequest)
            .options(selectinload(RefundRequest.user))
            .where(RefundRequest.id == refund_id)
        )
        req = r.scalar_one_or_none()
        if req is None:
            return _redirect_to_refunds("err", f"Заявка #{refund_id} не найдена")

        current_status = req.status if isinstance(req.status, str) else req.status.value
        if current_status != RefundStatus.PENDING.value:
            return _redirect_to_refunds("err",
                                         f"Заявка #{refund_id} уже обработана")

        student_tg_id = req.user.tg_id if req.user else None
        order_id = req.order_id

        await update_refund_status(session, refund_id, RefundStatus.REJECTED,
                                    rejection_reason=reason_clean)

    if student_tg_id:
        await notify_telegram(
            student_tg_id,
            f"❌ <b>Ваш запрос на возврат отклонён</b>\n\n"
            f"Заказ #{order_id}\nПричина: {reason_clean}\n\n"
            f"Если есть вопросы — напишите в /support.",
        )

    await log_admin_action(
        admin_tg_id=admin_tg_id, action="reject_refund",
        target_type="refund", target_id=refund_id,
        ip_address=get_client_ip(request),
        user_agent=request.headers.get("user-agent", ""),
        details={"reason": reason_clean, "order_id": order_id},
    )

    return _redirect_to_refunds("ok", f"Возврат #{refund_id} отклонён. Студент уведомлён.")
