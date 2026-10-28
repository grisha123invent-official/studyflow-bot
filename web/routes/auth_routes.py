"""
Маршруты авторизации админ-панели.

GET  /admin/login   — форма ввода tg_id
POST /admin/login   — приём tg_id, генерация OTP, отправка через Telegram → редирект на /admin/verify
GET  /admin/verify  — форма ввода кода (только если tg_id в session-flash)
POST /admin/verify  — приём кода, проверка, создание session-cookie → редирект на /admin
POST /admin/logout  — выход
"""
import logging
from fastapi import APIRouter, Form, Request, status
from fastapi.responses import RedirectResponse, HTMLResponse

from web.templating import templates
from web.security.auth import (
    is_admin, can_request_new_code, generate_otp, send_otp_via_telegram,
    verify_otp, create_session_token, invalidate_otp,
)
from web.security.csrf import (
    generate_csrf_token, csrf_tokens_match, CSRF_COOKIE_NAME, CSRF_FORM_FIELD,
)
from web.security.rate_limit import limiter, get_client_ip
from web.deps import SESSION_COOKIE_NAME, get_current_admin_id
from web.audit import log_admin_action
from config import settings

logger = logging.getLogger("admin.auth_routes")

router = APIRouter()


# ─── Хелперы ────────────────────────────────────────────────────────────────

def _set_csrf_cookie(response, token: str) -> None:
    """Устанавливает CSRF cookie с правильными флагами."""
    response.set_cookie(
        key=CSRF_COOKIE_NAME,
        value=token,
        httponly=True,
        secure=True,
        samesite="strict",
        max_age=24 * 3600,
        path="/admin",
    )


def _set_session_cookie(response, token: str) -> None:
    response.set_cookie(
        key=SESSION_COOKIE_NAME,
        value=token,
        httponly=True,
        secure=True,
        samesite="strict",
        max_age=settings.SESSION_TTL_HOURS * 3600,
        path="/admin",
    )


# ─── Login ──────────────────────────────────────────────────────────────────

@router.get("/admin/login", response_class=HTMLResponse)
async def login_form(request: Request):
    # Если уже авторизован — сразу редиректим на dashboard
    if get_current_admin_id(request):
        return RedirectResponse("/admin", status_code=status.HTTP_302_FOUND)

    csrf = generate_csrf_token()
    response = templates.TemplateResponse(
        request, "login.html",
        {"csrf_token": csrf, "error": request.query_params.get("error")},
    )
    _set_csrf_cookie(response, csrf)
    return response


@router.post("/admin/login")
@limiter.limit("5/minute")
async def login_submit(
    request: Request,
    tg_id: str = Form(...),
    csrf_token: str = Form(...),
):
    cookie_csrf = request.cookies.get(CSRF_COOKIE_NAME)
    if not csrf_tokens_match(cookie_csrf, csrf_token):
        return RedirectResponse("/admin/login?error=Session+expired,+try+again", status_code=303)

    tg_id_str = (tg_id or "").strip()
    if not tg_id_str.isdigit():
        return RedirectResponse("/admin/login?error=Telegram+ID+должен+быть+числом", status_code=303)

    tg_id_int = int(tg_id_str)
    client_ip = get_client_ip(request)
    user_agent = request.headers.get("user-agent", "")

    if not is_admin(tg_id_int):
        # Лог попытки + те же ответы что и для правильного ID (защита от перечисления)
        await log_admin_action(
            admin_tg_id=None,
            action="login_unauthorized_tg_id",
            ip_address=client_ip,
            user_agent=user_agent,
            details={"attempted_tg_id": tg_id_int},
        )
        # Чтобы атакующий не мог по таймингу узнать, валидный ли ID,
        # показываем тот же экран verify, но никакой код не отправляем.
        # Если бы перенаправили на login с ошибкой — атакующий бы знал что ID невалидный.
        # Но это плохой UX. Пусть лучше будет понятная ошибка.
        return RedirectResponse(
            "/admin/login?error=Этот+ID+не+админский.+Все+попытки+логируются.",
            status_code=303,
        )

    if not can_request_new_code(tg_id_int):
        return RedirectResponse(
            "/admin/login?error=Подождите+30+секунд+перед+повторным+запросом+кода",
            status_code=303,
        )

    # Генерируем код и шлём в Telegram
    code = generate_otp(tg_id_int)
    sent = await send_otp_via_telegram(tg_id_int, code)
    if not sent:
        invalidate_otp(tg_id_int)
        await log_admin_action(
            admin_tg_id=tg_id_int, action="otp_send_failed",
            ip_address=client_ip, user_agent=user_agent,
        )
        return RedirectResponse(
            "/admin/login?error=Не+удалось+отправить+код.+Попробуйте+позже.",
            status_code=303,
        )

    await log_admin_action(
        admin_tg_id=tg_id_int, action="otp_sent",
        ip_address=client_ip, user_agent=user_agent,
    )

    # Редирект на verify с tg_id в URL (короткий идентификатор сессии-flash)
    return RedirectResponse(f"/admin/verify?tg_id={tg_id_int}", status_code=303)


# ─── Verify ─────────────────────────────────────────────────────────────────

@router.get("/admin/verify", response_class=HTMLResponse)
async def verify_form(request: Request):
    tg_id = request.query_params.get("tg_id", "")
    if not tg_id.isdigit():
        return RedirectResponse("/admin/login", status_code=303)

    csrf = generate_csrf_token()
    response = templates.TemplateResponse(
        request, "verify.html",
        {
            "csrf_token": csrf,
            "tg_id": tg_id,
            "error": request.query_params.get("error"),
        },
    )
    _set_csrf_cookie(response, csrf)
    return response


@router.post("/admin/verify")
@limiter.limit("10/minute")
async def verify_submit(
    request: Request,
    tg_id: str = Form(...),
    code: str = Form(...),
    csrf_token: str = Form(...),
):
    cookie_csrf = request.cookies.get(CSRF_COOKIE_NAME)
    if not csrf_tokens_match(cookie_csrf, csrf_token):
        return RedirectResponse(f"/admin/verify?tg_id={tg_id}&error=Session+expired", status_code=303)

    if not tg_id.isdigit():
        return RedirectResponse("/admin/login", status_code=303)
    tg_id_int = int(tg_id)

    client_ip = get_client_ip(request)
    user_agent = request.headers.get("user-agent", "")

    if not is_admin(tg_id_int):
        return RedirectResponse("/admin/login?error=Доступ+запрещён", status_code=303)

    success, error_msg = verify_otp(tg_id_int, code)
    if not success:
        await log_admin_action(
            admin_tg_id=tg_id_int, action="login_failed",
            ip_address=client_ip, user_agent=user_agent,
            details={"reason": error_msg},
        )
        from urllib.parse import quote
        return RedirectResponse(
            f"/admin/verify?tg_id={tg_id_int}&error={quote(error_msg)}",
            status_code=303,
        )

    # Успех! Выдаём сессионную cookie
    session_token = create_session_token(tg_id_int)
    response = RedirectResponse("/admin", status_code=303)
    _set_session_cookie(response, session_token)

    await log_admin_action(
        admin_tg_id=tg_id_int, action="login_success",
        ip_address=client_ip, user_agent=user_agent,
    )
    return response


# ─── Logout ─────────────────────────────────────────────────────────────────

@router.post("/admin/logout")
async def logout(request: Request):
    tg_id = get_current_admin_id(request)
    client_ip = get_client_ip(request)
    if tg_id is not None:
        await log_admin_action(
            admin_tg_id=tg_id, action="logout",
            ip_address=client_ip,
            user_agent=request.headers.get("user-agent", ""),
        )

    response = RedirectResponse("/admin/login", status_code=303)
    response.delete_cookie(SESSION_COOKIE_NAME, path="/admin")
    response.delete_cookie(CSRF_COOKIE_NAME, path="/admin")
    return response
