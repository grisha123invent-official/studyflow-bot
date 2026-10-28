"""
Действия по пользователям.

POST /admin/users/{tg_id}/ban           — заблокировать (с причиной)
POST /admin/users/{tg_id}/unban         — разблокировать
POST /admin/users/{tg_id}/credit-bonus  — ручное начисление реф-бонуса
"""
import logging

from fastapi import APIRouter, Depends, Request, Form
from fastapi.responses import RedirectResponse
from sqlalchemy import select

from db.crud import ban_user, unban_user, credit_ref_balance_manual
from db.models import User
from db.session import AsyncSessionLocal
from web.audit import log_admin_action
from web.deps import require_admin, csrf_check
from web.flash import set_flash
from web.notifications import notify_telegram
from web.security.rate_limit import limiter, get_client_ip

logger = logging.getLogger("admin.user_actions")
router = APIRouter()


def _redirect_to_user(tg_id: int, level: str, message: str) -> RedirectResponse:
    response = RedirectResponse(f"/admin/users/{tg_id}", status_code=303)
    set_flash(response, level, message)
    return response


@router.post("/admin/users/{tg_id}/ban")
@limiter.limit("30/minute")
async def ban(
    request: Request,
    tg_id: int,
    reason: str = Form(...),
    csrf_token: str = Form(...),
    admin_tg_id: int = Depends(require_admin),
    _csrf: None = Depends(csrf_check),
):
    reason_clean = (reason or "").strip()
    if len(reason_clean) < 3:
        return _redirect_to_user(tg_id, "err", "Причина блокировки минимум 3 символа")

    if tg_id == admin_tg_id:
        return _redirect_to_user(tg_id, "err", "Нельзя забанить самого себя")

    async with AsyncSessionLocal() as session:
        r = await session.execute(select(User).where(User.tg_id == tg_id))
        user = r.scalar_one_or_none()
        if user is None:
            return _redirect_to_user(tg_id, "err", f"Пользователь {tg_id} не найден")
        if user.is_banned:
            return _redirect_to_user(tg_id, "err", "Пользователь уже заблокирован")

        await ban_user(session, tg_id)

    # Уведомление шлём ДО логирования: если бот заблокирован — поймаем 403 и пометим
    # (но юзер уже забанен у нас в БД)
    await notify_telegram(
        tg_id,
        f"🚫 <b>Ваш аккаунт заблокирован</b>\n\n"
        f"Причина: {reason_clean}\n\n"
        f"Если считаете блокировку ошибочной — напишите на {settings.SUPPORT_EMAIL}",
    )

    await log_admin_action(
        admin_tg_id=admin_tg_id, action="ban_user",
        target_type="user", target_id=tg_id,
        ip_address=get_client_ip(request),
        user_agent=request.headers.get("user-agent", ""),
        details={"reason": reason_clean},
    )

    return _redirect_to_user(tg_id, "ok", f"Пользователь {tg_id} заблокирован.")


@router.post("/admin/users/{tg_id}/unban")
@limiter.limit("30/minute")
async def unban(
    request: Request,
    tg_id: int,
    csrf_token: str = Form(...),
    admin_tg_id: int = Depends(require_admin),
    _csrf: None = Depends(csrf_check),
):
    async with AsyncSessionLocal() as session:
        r = await session.execute(select(User).where(User.tg_id == tg_id))
        user = r.scalar_one_or_none()
        if user is None:
            return _redirect_to_user(tg_id, "err", f"Пользователь {tg_id} не найден")
        if not user.is_banned:
            return _redirect_to_user(tg_id, "err", "Пользователь не заблокирован")

        await unban_user(session, tg_id)

    await notify_telegram(
        tg_id,
        f"✅ <b>Ваш аккаунт разблокирован</b>\n\n"
        f"Можете продолжать работу с ботом.",
    )

    await log_admin_action(
        admin_tg_id=admin_tg_id, action="unban_user",
        target_type="user", target_id=tg_id,
        ip_address=get_client_ip(request),
        user_agent=request.headers.get("user-agent", ""),
    )

    return _redirect_to_user(tg_id, "ok", f"Пользователь {tg_id} разблокирован.")


@router.post("/admin/users/{tg_id}/credit-bonus")
@limiter.limit("30/minute")
async def credit_bonus(
    request: Request,
    tg_id: int,
    amount: float = Form(...),
    reason: str = Form(...),
    csrf_token: str = Form(...),
    admin_tg_id: int = Depends(require_admin),
    _csrf: None = Depends(csrf_check),
):
    reason_clean = (reason or "").strip()
    if len(reason_clean) < 5:
        return _redirect_to_user(tg_id, "err", "Причина начисления минимум 5 символов")
    if amount <= 0 or amount > 50000:
        return _redirect_to_user(tg_id, "err", "Сумма должна быть от 1 до 50 000 ₽")

    async with AsyncSessionLocal() as session:
        ok = await credit_ref_balance_manual(session, tg_id, amount)
        if not ok:
            return _redirect_to_user(tg_id, "err", f"Пользователь {tg_id} не найден")

    await notify_telegram(
        tg_id,
        f"🎁 <b>Вам начислено {amount:.0f} ₽ на реф-баланс</b>\n\n"
        f"Причина: {reason_clean}\n\n"
        f"Посмотреть баланс: /ref",
    )

    await log_admin_action(
        admin_tg_id=admin_tg_id, action="credit_manual_bonus",
        target_type="user", target_id=tg_id,
        ip_address=get_client_ip(request),
        user_agent=request.headers.get("user-agent", ""),
        details={"amount": amount, "reason": reason_clean},
    )

    return _redirect_to_user(tg_id, "ok", f"Начислено {amount:.0f} ₽ юзеру {tg_id}.")
