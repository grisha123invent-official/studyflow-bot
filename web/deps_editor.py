"""
FastAPI dependencies для веб-панели редактора.

require_editor — проверка editor-session cookie → tg_id редактора.
csrf_check_editor — проверка CSRF для всех POST/PUT/DELETE.

Зеркало `web/deps.py`, но с другими salt'ами и cookie-именами,
чтобы admin-cookie не давала доступ к /editor/* и наоборот.
"""
import logging
from typing import Optional

from fastapi import HTTPException, Request, status
from itsdangerous import URLSafeTimedSerializer, BadSignature, SignatureExpired

from config import settings
from web.security.csrf import csrf_tokens_match, CSRF_FORM_FIELD, CSRF_HEADER_NAME
from web.security.roles import is_editor

logger = logging.getLogger("editor.deps")

EDITOR_SESSION_COOKIE_NAME = "pishu24_editor_session"
EDITOR_CSRF_COOKIE_NAME = "editor_csrf_token"


# ─── Editor session signer ──────────────────────────────────────────────────

def _editor_signer() -> URLSafeTimedSerializer:
    if not settings.SESSION_SECRET:
        raise RuntimeError("SESSION_SECRET не задан в .env")
    return URLSafeTimedSerializer(settings.SESSION_SECRET, salt="pishu24-editor-session")


def create_editor_session_token(tg_id: int) -> str:
    return _editor_signer().dumps({"tg_id": tg_id})


def verify_editor_session_token(token: Optional[str]) -> Optional[int]:
    """Проверяет editor-токен. Возвращает tg_id или None."""
    if not token:
        return None
    try:
        max_age = settings.SESSION_TTL_HOURS * 3600
        data = _editor_signer().loads(token, max_age=max_age)
        tg_id = data.get("tg_id")
        if not isinstance(tg_id, int):
            return None
        if not is_editor(tg_id):
            return None
        return tg_id
    except (BadSignature, SignatureExpired):
        return None
    except Exception as e:
        logger.warning(f"Editor session verification error: {e}")
        return None


# ─── Dependencies ───────────────────────────────────────────────────────────

def get_current_editor_id(request: Request) -> Optional[int]:
    """Возвращает tg_id текущего редактора или None."""
    token = request.cookies.get(EDITOR_SESSION_COOKIE_NAME)
    return verify_editor_session_token(token)


def require_editor(request: Request) -> int:
    """Dependency: бросает 307 → /editor/login если не авторизован."""
    tg_id = get_current_editor_id(request)
    if tg_id is None:
        raise HTTPException(
            status_code=status.HTTP_307_TEMPORARY_REDIRECT,
            headers={"Location": "/editor/login"},
        )
    if not is_editor(tg_id):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Доступ запрещён",
        )
    return tg_id


async def csrf_check_editor(request: Request) -> None:
    """CSRF-проверка для редакторских POST'ов (своя cookie)."""
    if request.method not in ("POST", "PUT", "PATCH", "DELETE"):
        return

    cookie_token = request.cookies.get(EDITOR_CSRF_COOKIE_NAME)

    submitted = request.headers.get(CSRF_HEADER_NAME)
    if not submitted:
        try:
            form = await request.form()
            submitted = form.get(CSRF_FORM_FIELD)
        except Exception:
            submitted = None

    if not csrf_tokens_match(cookie_token, submitted):
        logger.warning(
            f"Editor CSRF check failed: path={request.url.path} "
            f"method={request.method} ip={request.client.host if request.client else '?'}"
        )
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="CSRF token mismatch",
        )
