"""
Маршруты авторизации редакторской панели.

GET  /editor/login   — форма ввода tg_id
POST /editor/login   — приём tg_id, OTP, редирект на /editor/verify
GET  /editor/verify  — форма кода
POST /editor/verify  — приём кода → editor-session cookie → редирект на /editor
POST /editor/logout  — выход
"""
import logging
from fastapi import APIRouter, Form, Request, status
from fastapi.responses import RedirectResponse, HTMLResponse

from config import settings
from web.audit import log_admin_action
from web.deps_editor import (
    EDITOR_SESSION_COOKIE_NAME, EDITOR_CSRF_COOKIE_NAME,
    create_editor_session_token, get_current_editor_id,
)
from web.security.auth import (
    can_request_new_code, generate_otp, send_otp_via_telegram,
    verify_otp, invalidate_otp,
)
from web.security.csrf import (
    generate_csrf_token, csrf_tokens_match, CSRF_FORM_FIELD,
)
from web.security.rate_limit import limiter, get_client_ip
from web.security.roles import is_editor
from web.templating import templates

logger = logging.getLogger("editor.auth_routes")
router = APIRouter()


# ─── Хелперы cookie ──────────────────────────────────────────────────────────

def _set_csrf_cookie(response, token: str) -> None:
    response.set_cookie(
        key=EDITOR_CSRF_COOKIE_NAME, value=token,
        httponly=True, secure=True, samesite="strict",
        max_age=24 * 3600, path="/editor",
    )


def _set_session_cookie(response, token: str) -> None:
    response.set_cookie(
        key=EDITOR_SESSION_COOKIE_NAME, value=token,
        httponly=True, secure=True, samesite="strict",
        max_age=settings.SESSION_TTL_HOURS * 3600, path="/editor",
    )


# ─── Login ──────────────────────────────────────────────────────────────────

@router.get("/editor/login", response_class=HTMLResponse)
async def editor_login_form(request: Request):
    if get_current_editor_id(request):
        return RedirectResponse("/editor", status_code=302)

    csrf = generate_csrf_token()
    response = templates.TemplateResponse(
        request, "editor/login.html",
        {"csrf_token": csrf, "error": request.query_params.get("error")},
    )
    _set_csrf_cookie(response, csrf)
    return response


@router.post("/editor/login")
@limiter.limit("5/minute")
async def editor_login_submit(
    request: Request,
    tg_id: str = Form(...),
    csrf_token: str = Form(...),
):
    cookie_csrf = request.cookies.get(EDITOR_CSRF_COOKIE_NAME)
    if not csrf_tokens_match(cookie_csrf, csrf_token):
        return RedirectResponse("/editor/login?error=Session+expired,+try+again", status_code=303)

    tg_id_str = (tg_id or "").strip()
    if not tg_id_str.isdigit():
        return RedirectResponse("/editor/login?error=Telegram+ID+должен+быть+числом", status_code=303)

    tg_id_int = int(tg_id_str)
    client_ip = get_client_ip(request)
    user_agent = request.headers.get("user-agent", "")

    if not is_editor(tg_id_int):
        await log_admin_action(
            admin_tg_id=None, action="editor_login_unauthorized_tg_id",
            ip_address=client_ip, user_agent=user_agent,
            details={"attempted_tg_id": tg_id_int},
        )
        return RedirectResponse(
            "/editor/login?error=Этот+ID+не+редакторский.+Все+попытки+логируются.",
            status_code=303,
        )

    if not can_request_new_code(tg_id_int):
        return RedirectResponse(
            "/editor/login?error=Подождите+30+секунд+перед+повторным+запросом+кода",
            status_code=303,
        )

    code = generate_otp(tg_id_int)
    sent = await send_otp_via_telegram(tg_id_int, code)
    if not sent:
        invalidate_otp(tg_id_int)
        await log_admin_action(
            admin_tg_id=tg_id_int, action="editor_otp_send_failed",
            ip_address=client_ip, user_agent=user_agent,
        )
        return RedirectResponse(
            "/editor/login?error=Не+удалось+отправить+код.+Попробуйте+позже.",
            status_code=303,
        )

    await log_admin_action(
        admin_tg_id=tg_id_int, action="editor_otp_sent",
        ip_address=client_ip, user_agent=user_agent,
    )

    return RedirectResponse(f"/editor/verify?tg_id={tg_id_int}", status_code=303)


# ─── Verify ─────────────────────────────────────────────────────────────────

@router.get("/editor/verify", response_class=HTMLResponse)
async def editor_verify_form(request: Request):
    tg_id = request.query_params.get("tg_id", "")
    if not tg_id.isdigit():
        return RedirectResponse("/editor/login", status_code=303)

    csrf = generate_csrf_token()
    response = templates.TemplateResponse(
        request, "editor/verify.html",
        {
            "csrf_token": csrf,
            "tg_id": tg_id,
            "error": request.query_params.get("error"),
        },
    )
    _set_csrf_cookie(response, csrf)
    return response


@router.post("/editor/verify")
@limiter.limit("10/minute")
async def editor_verify_submit(
    request: Request,
    tg_id: str = Form(...),
    code: str = Form(...),
    csrf_token: str = Form(...),
):
    cookie_csrf = request.cookies.get(EDITOR_CSRF_COOKIE_NAME)
    if not csrf_tokens_match(cookie_csrf, csrf_token):
        return RedirectResponse(f"/editor/verify?tg_id={tg_id}&error=Session+expired", status_code=303)

    if not tg_id.isdigit():
        return RedirectResponse("/editor/login", status_code=303)
    tg_id_int = int(tg_id)

    client_ip = get_client_ip(request)
    user_agent = request.headers.get("user-agent", "")

    if not is_editor(tg_id_int):
        return RedirectResponse("/editor/login?error=Доступ+запрещён", status_code=303)

    success, error_msg = verify_otp(tg_id_int, code)
    if not success:
        await log_admin_action(
            admin_tg_id=tg_id_int, action="editor_login_failed",
            ip_address=client_ip, user_agent=user_agent,
            details={"reason": error_msg},
        )
        from urllib.parse import quote
        return RedirectResponse(
            f"/editor/verify?tg_id={tg_id_int}&error={quote(error_msg)}",
            status_code=303,
        )

    session_token = create_editor_session_token(tg_id_int)
    response = RedirectResponse("/editor", status_code=303)
    _set_session_cookie(response, session_token)

    await log_admin_action(
        admin_tg_id=tg_id_int, action="editor_login_success",
        ip_address=client_ip, user_agent=user_agent,
    )
    return response


# ─── Logout ─────────────────────────────────────────────────────────────────

@router.post("/editor/logout")
async def editor_logout(request: Request):
    tg_id = get_current_editor_id(request)
    client_ip = get_client_ip(request)
    if tg_id is not None:
        await log_admin_action(
            admin_tg_id=tg_id, action="editor_logout",
            ip_address=client_ip,
            user_agent=request.headers.get("user-agent", ""),
        )

    response = RedirectResponse("/editor/login", status_code=303)
    response.delete_cookie(EDITOR_SESSION_COOKIE_NAME, path="/editor")
    response.delete_cookie(EDITOR_CSRF_COOKIE_NAME, path="/editor")
    return response
