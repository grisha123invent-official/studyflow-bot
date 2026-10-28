"""
Тесты редакторской веб-панели.

Покрывают: is_editor(), student_alias(), editor session token signing,
анонимизацию карточки задания, защиту скачивания файлов.
"""
import pytest
from unittest.mock import patch


# ─── is_editor ──────────────────────────────────────────────────────────────

def test_is_editor_recognizes_editor_tg_id(monkeypatch):
    from config import settings
    monkeypatch.setattr(settings, "EDITOR_TG_ID", 12345)
    from web.security.roles import is_editor
    assert is_editor(12345) is True


def test_is_editor_rejects_other_tg_id(monkeypatch):
    from config import settings
    monkeypatch.setattr(settings, "EDITOR_TG_ID", 12345)
    from web.security.roles import is_editor
    assert is_editor(99999) is False
    assert is_editor(0) is False


def test_is_editor_returns_false_when_editor_id_unset(monkeypatch):
    from config import settings
    monkeypatch.setattr(settings, "EDITOR_TG_ID", None)
    from web.security.roles import is_editor
    assert is_editor(12345) is False


# ─── student_alias ──────────────────────────────────────────────────────────

def test_student_alias_format():
    from web.anonymize import student_alias
    assert student_alias(42) == "student_0042"
    assert student_alias(1) == "student_0001"
    assert student_alias(99999) == "student_99999"


def test_student_alias_zero_id():
    from web.anonymize import student_alias
    assert student_alias(0) == "student_0000"


# ─── Editor session token ───────────────────────────────────────────────────

def test_editor_session_token_roundtrip(monkeypatch):
    from config import settings
    monkeypatch.setattr(settings, "SESSION_SECRET", "test_secret_for_unit_xxx" * 2)
    monkeypatch.setattr(settings, "SESSION_TTL_HOURS", 24)
    monkeypatch.setattr(settings, "EDITOR_TG_ID", 7777)

    from web.deps_editor import create_editor_session_token, verify_editor_session_token

    token = create_editor_session_token(7777)
    assert isinstance(token, str)
    assert verify_editor_session_token(token) == 7777


def test_editor_session_rejects_non_editor(monkeypatch):
    """Токен валидный, но tg_id не в whitelist EDITOR_TG_ID → None."""
    from config import settings
    monkeypatch.setattr(settings, "SESSION_SECRET", "test_secret_for_unit_xxx" * 2)
    monkeypatch.setattr(settings, "SESSION_TTL_HOURS", 24)
    monkeypatch.setattr(settings, "EDITOR_TG_ID", 7777)

    from web.deps_editor import create_editor_session_token, verify_editor_session_token

    token = create_editor_session_token(7777)
    # Меняем whitelist — старый токен больше не валиден
    monkeypatch.setattr(settings, "EDITOR_TG_ID", 9999)
    assert verify_editor_session_token(token) is None


def test_editor_session_rejects_tampered_token(monkeypatch):
    from config import settings
    monkeypatch.setattr(settings, "SESSION_SECRET", "test_secret_for_unit_xxx" * 2)
    monkeypatch.setattr(settings, "EDITOR_TG_ID", 7777)

    from web.deps_editor import verify_editor_session_token

    assert verify_editor_session_token("tampered.garbage.value") is None
    assert verify_editor_session_token("") is None
    assert verify_editor_session_token(None) is None


def test_editor_session_isolated_from_admin_session(monkeypatch):
    """Admin-token с salt 'pishu24-admin-session' не должен валидироваться editor-функцией."""
    from config import settings
    monkeypatch.setattr(settings, "SESSION_SECRET", "test_secret_for_unit_xxx" * 2)
    monkeypatch.setattr(settings, "SESSION_TTL_HOURS", 24)
    monkeypatch.setattr(settings, "EDITOR_TG_ID", 7777)
    monkeypatch.setattr(settings, "ADMIN_TG_IDS", [7777])  # тот же tg_id — но другой salt

    from web.security.auth import create_session_token as create_admin_token
    from web.deps_editor import verify_editor_session_token

    admin_token = create_admin_token(7777)
    # Admin токен подписан admin-salt — editor верификатор должен его отклонить
    assert verify_editor_session_token(admin_token) is None


# ─── Анонимизация карточки задания ──────────────────────────────────────────

def test_serialize_task_has_no_pii():
    """Сериализатор задания НЕ должен содержать username/email/phone/tg_id/full_name."""
    from datetime import datetime
    from web.routes.editor_tasks import _serialize_task_card

    # Моделируем Order с user_id
    class FakeOrder:
        id = 42
        user_id = 1001
        work_type = "Реферат"
        topic = "История"
        subject = "Высшая школа экономики"
        pages = 15
        deadline = "1 неделя"
        comment = "тестовый"
        status = "EDITING"
        revision_comment = None
        revisions_used = 0
        created_at = datetime.now()

    result = _serialize_task_card(FakeOrder())

    # Должны быть
    assert "id" in result
    assert "alias" in result
    assert result["alias"] == "student_1001"

    # НЕ должны быть
    pii_keys = {"username", "email", "phone", "tg_id", "full_name", "user_id_raw"}
    leaked = pii_keys & set(result.keys())
    assert not leaked, f"Утечка PII: {leaked}"


# ─── Защита скачивания файлов ───────────────────────────────────────────────

def test_editor_files_allowed_types():
    """Тип PREVIEW отдаётся inline, остальные — attachment."""
    from db.models import FileType
    from web.routes.editor_files import ALLOWED_FILE_TYPES

    assert FileType.STUDENT_TEMPLATE.value in ALLOWED_FILE_TYPES
    assert FileType.DRAFT.value in ALLOWED_FILE_TYPES
    assert FileType.PREVIEW.value in ALLOWED_FILE_TYPES
    assert FileType.FINAL.value in ALLOWED_FILE_TYPES


def test_editor_files_disallows_done_order():
    """Заказы в DONE недоступны для скачивания файлов редактору."""
    from db.models import OrderStatus
    from web.routes.editor_files import ALLOWED_ORDER_STATUSES

    assert OrderStatus.DONE.value not in ALLOWED_ORDER_STATUSES
    assert OrderStatus.CANCELLED.value not in ALLOWED_ORDER_STATUSES
    assert OrderStatus.REFUNDED.value not in ALLOWED_ORDER_STATUSES
    assert OrderStatus.PENDING.value not in ALLOWED_ORDER_STATUSES
    # Но эти — разрешены
    assert OrderStatus.EDITING.value in ALLOWED_ORDER_STATUSES
    assert OrderStatus.PREVIEW_SENT.value in ALLOWED_ORDER_STATUSES
    assert OrderStatus.AWAITING_PAYMENT.value in ALLOWED_ORDER_STATUSES


# ─── Editor flash ───────────────────────────────────────────────────────────

def test_editor_flash_roundtrip(monkeypatch):
    from config import settings
    if not settings.CSRF_SECRET:
        monkeypatch.setattr(settings, "CSRF_SECRET", "test_secret_for_unit_xx" * 2)

    from web.flash_editor import set_editor_flash, pop_editor_flash

    class FakeResponse:
        def __init__(self):
            self.cookies = {}
        def set_cookie(self, key, value, **kwargs):
            self.cookies[key] = value
        def delete_cookie(self, key, **kw):
            self.cookies.pop(key, None)

    resp = FakeResponse()
    set_editor_flash(resp, "ok", "Превью отправлено")

    class FakeRequest:
        def __init__(self, cookies):
            self.cookies = cookies

    req = FakeRequest({"pishu24_editor_flash": resp.cookies["pishu24_editor_flash"]})
    payload = pop_editor_flash(req)
    assert payload is not None
    assert payload["level"] == "ok"
    assert payload["message"] == "Превью отправлено"


def test_editor_flash_isolated_from_admin_flash(monkeypatch):
    """Admin-flash cookie с другим salt не должна читаться editor-popper'ом."""
    from config import settings
    if not settings.CSRF_SECRET:
        monkeypatch.setattr(settings, "CSRF_SECRET", "test_secret_for_unit_xx" * 2)

    from web.flash import set_flash
    from web.flash_editor import pop_editor_flash

    class FakeResponse:
        def __init__(self): self.cookies = {}
        def set_cookie(self, key, value, **kw): self.cookies[key] = value
        def delete_cookie(self, key, **kw): pass

    resp = FakeResponse()
    set_flash(resp, "ok", "admin msg")  # admin flash

    # admin cookie name = "pishu24_flash", editor читает из "pishu24_editor_flash"
    # → редактор просто не увидит admin-сообщение
    class FakeRequest:
        cookies = {"pishu24_flash": resp.cookies["pishu24_flash"]}

    assert pop_editor_flash(FakeRequest()) is None
