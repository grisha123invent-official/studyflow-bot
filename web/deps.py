"""
FastAPI dependencies для админ-панели.

require_admin — проверяет сессионную cookie и возвращает tg_id админа.
                Если не авторизован → RedirectResponse на /admin/login.

csrf_check — для всех POST/PUT/DELETE проверяет CSRF-токен.
"""
import logging
from typing import Optional

from fastapi import HTTPException, Request, status
from fastapi.responses import RedirectResponse

from web.security.auth import verify_session_token, is_admin
from web.security.csrf import csrf_tokens_match, CSRF_COOKIE_NAME, CSRF_FORM_FIELD, CSRF_HEADER_NAME

logger = logging.getLogger("admin.deps")


SESSION_COOKIE_NAME = "pishu24_admin_session"


def get_current_admin_id(request: Request) -> Optional[int]:
    """Возвращает tg_id текущего админа или None если не авторизован."""
    token = request.cookies.get(SESSION_COOKIE_NAME)
    return verify_session_token(token)


def require_admin(request: Request) -> int:
    """
    Dependency для всех защищённых маршрутов.
    Если не авторизован — бросает HTTPException с redirect.
    """
    tg_id = get_current_admin_id(request)
    if tg_id is None:
        # FastAPI поддерживает 307 redirect через HTTPException, но для UX лучше
        # просто возвращать 401 и пусть фронт делает редирект,
        # либо сразу 307 со ссылкой
        raise HTTPException(
            status_code=status.HTTP_307_TEMPORARY_REDIRECT,
            headers={"Location": "/admin/login"},
        )
    if not is_admin(tg_id):
        # Cookie валидна, но tg_id выпал из whitelist — отказ
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Доступ запрещён",
        )
    return tg_id


async def csrf_check(request: Request) -> None:
    """
    Проверяет CSRF на всех state-changing методах.
    Подключается к роутам через Depends(csrf_check).
    """
    if request.method not in ("POST", "PUT", "PATCH", "DELETE"):
        return  # safe methods не требуют CSRF

    cookie_token = request.cookies.get(CSRF_COOKIE_NAME)

    # Сначала пробуем заголовок (для HTMX/JS), затем форму
    submitted = request.headers.get(CSRF_HEADER_NAME)
    if not submitted:
        try:
            form = await request.form()
            submitted = form.get(CSRF_FORM_FIELD)
        except Exception:
            submitted = None

    if not csrf_tokens_match(cookie_token, submitted):
        logger.warning(
            f"CSRF check failed: path={request.url.path} "
            f"method={request.method} ip={request.client.host if request.client else '?'}"
        )
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="CSRF token mismatch",
        )
