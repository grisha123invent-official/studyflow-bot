"""
Общая инициализация Jinja2 для админ-панели.

На Python 3.14 в стандартном FastAPI Jinja2Templates возникает баг
`TypeError: cannot use 'tuple' as a dict key (unhashable type: 'dict')`
из-за изменений в кешировании Jinja2. Обходим, отключая внутренний кеш
шаблонов (`cache_size=0`).
"""
import json
import logging
from typing import Optional

from jinja2 import Environment, FileSystemLoader, select_autoescape
from fastapi import Request
from fastapi.responses import Response
from fastapi.templating import Jinja2Templates
from itsdangerous import URLSafeTimedSerializer, BadSignature

from config import settings

logger = logging.getLogger("admin.templating")


_env = Environment(
    loader=FileSystemLoader(["web/templates", "landing"]),
    autoescape=select_autoescape(["html", "htm", "xml"]),
    cache_size=0,            # обход бага Python 3.14 + Jinja2
    auto_reload=True,
    enable_async=False,
)

# Пробрасываем white-label настройки во все шаблоны Jinja2
_env.globals["brand_name"] = settings.BRAND_NAME
_env.globals["bot_username"] = settings.BOT_USERNAME
_env.globals["support_email"] = settings.SUPPORT_EMAIL
_env.globals["owner_name"] = settings.OWNER_NAME
_env.globals["owner_inn"] = settings.OWNER_INN
_env.globals["owner_city"] = settings.OWNER_CITY
_env.globals["public_url"] = settings.PUBLIC_URL

templates = Jinja2Templates(env=_env)


def _read_flash_from_request(request: Request) -> Optional[dict]:
    """Читает flash-cookie без её удаления (удаление — отдельно через response)."""
    token = request.cookies.get("pishu24_flash")
    if not token:
        return None
    try:
        signer = URLSafeTimedSerializer(settings.CSRF_SECRET, salt="pishu24-admin-flash")
        payload_str = signer.loads(token, max_age=60)
        return json.loads(payload_str)
    except (BadSignature, Exception) as e:
        logger.debug(f"Bad flash cookie: {e}")
        return None


def render(request: Request, name: str, ctx: dict, status_code: int = 200) -> Response:
    """
    Обёртка над templates.TemplateResponse, которая прокидывает flash-сообщение
    из cookie в template context и удаляет cookie после показа.
    """
    flash_data = _read_flash_from_request(request)
    if flash_data:
        ctx = dict(ctx)
        ctx["flash"] = flash_data

    response = templates.TemplateResponse(
        request, name, ctx, status_code=status_code,
    )

    if flash_data:
        # Удаляем cookie — flash одноразовый
        response.delete_cookie("pishu24_flash", path="/admin")

    return response


def render_editor(request: Request, name: str, ctx: dict, status_code: int = 200) -> Response:
    """
    То же что render(), но для редакторской панели — другая cookie для flash.
    """
    from web.flash_editor import pop_editor_flash, EDITOR_FLASH_COOKIE

    flash_data = pop_editor_flash(request)
    if flash_data:
        ctx = dict(ctx)
        ctx["flash"] = flash_data

    response = templates.TemplateResponse(
        request, name, ctx, status_code=status_code,
    )

    if flash_data:
        response.delete_cookie(EDITOR_FLASH_COOKIE, path="/editor")

    return response
