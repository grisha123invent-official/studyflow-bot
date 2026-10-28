"""
Flash-сообщения для редакторской панели — отдельная cookie от админской.
"""
import json
import logging
from typing import Literal, Optional

from fastapi import Request
from fastapi.responses import Response
from itsdangerous import URLSafeTimedSerializer, BadSignature

from config import settings

logger = logging.getLogger("editor.flash")

EDITOR_FLASH_COOKIE = "pishu24_editor_flash"
FLASH_TTL_SECONDS = 60
FlashLevel = Literal["ok", "err", "info"]


def _signer() -> URLSafeTimedSerializer:
    if not settings.CSRF_SECRET:
        raise RuntimeError("CSRF_SECRET не задан в .env")
    return URLSafeTimedSerializer(settings.CSRF_SECRET, salt="pishu24-editor-flash")


def set_editor_flash(response: Response, level: FlashLevel, message: str) -> None:
    payload = json.dumps({"level": level, "message": message}, ensure_ascii=False)
    token = _signer().dumps(payload)
    response.set_cookie(
        key=EDITOR_FLASH_COOKIE,
        value=token,
        max_age=FLASH_TTL_SECONDS,
        httponly=True,
        secure=True,
        samesite="strict",
        path="/editor",
    )


def pop_editor_flash(request: Request) -> Optional[dict]:
    """Читает flash из cookie (не удаляет — это делает render_editor через response)."""
    token = request.cookies.get(EDITOR_FLASH_COOKIE)
    if not token:
        return None
    try:
        payload_str = _signer().loads(token, max_age=FLASH_TTL_SECONDS)
        return json.loads(payload_str)
    except (BadSignature, Exception) as e:
        logger.debug(f"Bad editor flash cookie: {e}")
        return None
