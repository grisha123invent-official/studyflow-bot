"""
CSRF protection.

Используем стратегию double-submit-cookie:
- При первом GET сервер выставляет cookie `csrf_token` с подписанным случайным токеном
- При POST-запросе клиент обязан передать тот же токен через скрытое поле формы (или заголовок X-CSRF-Token)
- Сервер сравнивает значение из cookie со значением из формы → если не совпадает, отказывает (403)

Это защищает от классического CSRF: атакующий сайт не может прочитать cookie
жертвы (SameSite=Strict), значит не может подделать запрос с правильным токеном.
"""
import logging
import secrets
from typing import Optional

from itsdangerous import URLSafeTimedSerializer, BadSignature

from config import settings

logger = logging.getLogger("admin.csrf")

CSRF_COOKIE_NAME = "csrf_token"
CSRF_FORM_FIELD = "csrf_token"
CSRF_HEADER_NAME = "x-csrf-token"
CSRF_TTL_SECONDS = 24 * 3600  # 24 часа


def _get_signer() -> URLSafeTimedSerializer:
    if not settings.CSRF_SECRET:
        raise RuntimeError("CSRF_SECRET не задан в .env")
    return URLSafeTimedSerializer(settings.CSRF_SECRET, salt="pishu24-admin-csrf")


def generate_csrf_token() -> str:
    """Генерирует новый подписанный CSRF-токен."""
    nonce = secrets.token_urlsafe(32)
    return _get_signer().dumps(nonce)


def verify_csrf_token(token: Optional[str]) -> bool:
    """Проверяет CSRF-токен."""
    if not token:
        return False
    try:
        _get_signer().loads(token, max_age=CSRF_TTL_SECONDS)
        return True
    except (BadSignature, Exception):
        return False


def csrf_tokens_match(cookie_token: Optional[str], submitted_token: Optional[str]) -> bool:
    """
    Проверяет совпадение CSRF-токенов (из cookie и из формы/заголовка).
    Использует постоянное по времени сравнение.
    """
    if not cookie_token or not submitted_token:
        return False
    # Сначала проверяем подпись обоих токенов
    if not verify_csrf_token(cookie_token) or not verify_csrf_token(submitted_token):
        return False
    return secrets.compare_digest(cookie_token, submitted_token)
