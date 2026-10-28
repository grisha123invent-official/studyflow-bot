"""
Flash-сообщения через short-lived signed cookie.

Паттерн:
1. Action-роут после успеха: response = RedirectResponse(...); set_flash(response, 'ok', '...')
2. GET-роут (или middleware): pop_flash(request, response) → передать в template context

Cookie живёт 60 секунд (хватает на один redirect), подписана тем же ключом,
что и CSRF, но другой salt. После чтения — сразу удаляется.
"""
import json
import logging
from typing import Literal, Optional

from fastapi import Request
from fastapi.responses import Response
from itsdangerous import URLSafeTimedSerializer, BadSignature

from config import settings

logger = logging.getLogger("admin.flash")

FLASH_COOKIE_NAME = "pishu24_flash"
FLASH_TTL_SECONDS = 60
FlashLevel = Literal["ok", "err", "info"]


def _signer() -> URLSafeTimedSerializer:
    if not settings.CSRF_SECRET:
        raise RuntimeError("CSRF_SECRET не задан в .env")
    return URLSafeTimedSerializer(settings.CSRF_SECRET, salt="pishu24-admin-flash")


def set_flash(response: Response, level: FlashLevel, message: str) -> None:
    """Кладёт flash-сообщение в подписанную cookie."""
    payload = json.dumps({"level": level, "message": message}, ensure_ascii=False)
    token = _signer().dumps(payload)
    response.set_cookie(
        key=FLASH_COOKIE_NAME,
        value=token,
        max_age=FLASH_TTL_SECONDS,
        httponly=True,
        secure=True,
        samesite="strict",
        path="/admin",
    )


def pop_flash(request: Request, response: Response) -> Optional[dict]:
    """
    Читает flash-сообщение из cookie и сразу удаляет cookie.
    Возвращает {'level': ..., 'message': ...} или None.
    """
    token = request.cookies.get(FLASH_COOKIE_NAME)
    if not token:
        return None
    try:
        payload_str = _signer().loads(token, max_age=FLASH_TTL_SECONDS)
        payload = json.loads(payload_str)
    except (BadSignature, Exception) as e:
        logger.debug(f"Bad flash cookie: {e}")
        response.delete_cookie(FLASH_COOKIE_NAME, path="/admin")
        return None

    # Очищаем cookie после чтения (одноразовое)
    response.delete_cookie(FLASH_COOKIE_NAME, path="/admin")
    return payload
