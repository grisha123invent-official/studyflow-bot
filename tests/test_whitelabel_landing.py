"""
Тесты White-Label рендеринга лендинга и юридических страниц через FastAPI.
"""
import asyncio
from starlette.requests import Request
from web.main import (
    landing_index,
    landing_oferta,
    landing_privacy,
    landing_contacts,
    healthcheck,
)
from config import settings


def _make_request(path: str = "/") -> Request:
    scope = {
        "type": "http",
        "method": "GET",
        "path": path,
        "headers": [],
        "query_string": b"",
        "client": ("127.0.0.1", 12345),
    }
    return Request(scope)


def test_whitelabel_landing_pages_render_settings():
    resp_index = asyncio.run(landing_index(_make_request("/")))
    assert resp_index.status_code == 200
    body_index = resp_index.body.decode("utf-8")
    assert settings.BRAND_NAME in body_index
    assert settings.BOT_USERNAME in body_index
    assert settings.SUPPORT_EMAIL in body_index
    assert "Расчёт в боте" in body_index
    assert "ИНН:" not in body_index

    resp_oferta = asyncio.run(landing_oferta(_make_request("/oferta")))
    assert resp_oferta.status_code == 200
    body_oferta = resp_oferta.body.decode("utf-8")
    assert settings.OWNER_NAME in body_oferta
    assert settings.OWNER_INN in body_oferta

    resp_privacy = asyncio.run(landing_privacy(_make_request("/privacy")))
    assert resp_privacy.status_code == 200
    assert settings.SUPPORT_EMAIL in resp_privacy.body.decode("utf-8")

    resp_contacts = asyncio.run(landing_contacts(_make_request("/contacts")))
    assert resp_contacts.status_code == 200
    assert settings.BOT_USERNAME in resp_contacts.body.decode("utf-8")

    hc = asyncio.run(healthcheck())
    assert hc == {"status": "ok"}
