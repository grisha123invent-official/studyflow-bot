"""
Конфигурация бота и веб-платформы — загружает переменные окружения из .env
"""
import os
import secrets
from dataclasses import dataclass
from dotenv import load_dotenv

load_dotenv()


@dataclass
class Settings:
    BOT_TOKEN: str
    ADMIN_TG_ID: int
    EDITOR_TG_ID: int | None
    WEBHOOK_HOST: str

    # ─── White-label: Бренд и реквизиты исполнителя ──────────────────────
    BRAND_NAME: str = "StudyFlow"
    BOT_USERNAME: str = "your_study_bot"
    PUBLIC_URL: str = "https://example.com"
    SUPPORT_EMAIL: str = "support@example.com"
    OWNER_NAME: str = "Иванов Иван Иванович"
    OWNER_INN: str = "000000000000"
    OWNER_CITY: str = "Москва"
    WATERMARK_TEXT: str = "ОПЛАТИТЕ ДЛЯ ПОЛУЧЕНИЯ"

    # ─── Робокасса ───────────────────────────────────────────────────────
    ROBOKASSA_LOGIN: str = ""                # Идентификатор магазина
    ROBOKASSA_PASSWORD_1: str = ""           # Боевой пароль #1 — для подписи запросов на создание
    ROBOKASSA_PASSWORD_2: str = ""           # Боевой пароль #2 — для проверки Result URL
    ROBOKASSA_TEST_PASSWORD_1: str = ""      # Тестовый пароль #1
    ROBOKASSA_TEST_PASSWORD_2: str = ""      # Тестовый пароль #2
    ROBOKASSA_IS_TEST: bool = True           # True = тестовый режим, False = боевой
    ROBOKASSA_HASH_ALGO: str = "sha256"      # sha256 / md5 / sha384 / sha512

    # Email/телефон самозанятого для чеков 54-ФЗ
    # Используется как fallback, если у пользователя нет email/phone в БД
    ROBOKASSA_FALLBACK_EMAIL: str = "support@example.com"

    # ─── Веб-админка ────────────────────────────────────────────────────
    # Список Telegram ID администраторов, которым разрешён вход.
    # ВАЖНО: использовать tg_id (число), а не username (его можно поменять).
    # В .env: ADMIN_TG_IDS=123456789,987654321 (через запятую)
    ADMIN_TG_IDS: list[int] | None = None

    # Секрет для подписи cookie-сессий (itsdangerous). Сгенерировать:
    # `openssl rand -base64 32`. НИКОГДА не коммитить в репозиторий.
    SESSION_SECRET: str = ""
    # TTL сессии в часах
    SESSION_TTL_HOURS: int = 24

    # Секрет для CSRF-токенов
    CSRF_SECRET: str = ""

    # Порт для веб-админки (uvicorn)
    ADMIN_WEB_PORT: int = 8081

    # ─── ЮКасса (DEPRECATED, оставлено для совместимости) ────────────────
    YUKASSA_SHOP_ID: str = ""
    YUKASSA_SECRET_KEY: str = ""
    YUKASSA_FALLBACK_EMAIL: str = ""

    # Базовая цена за страницу — НЕ ИСПОЛЬЗУЕТСЯ.
    # Цены теперь берутся из services/pricing.py (синхронизировано с лендингом).
    PRICE_PER_PAGE: int = 100

    # Порт для webhook (Result URL от Робокассы)
    PAYMENT_WEBHOOK_PORT: int = 8080

    # Директория для загруженных файлов
    UPLOADS_DIR: str = "uploads"

    # Директория с шаблонами .docx
    TEMPLATES_DIR: str = "templates"


def _load_settings() -> Settings:
    bot_token = os.getenv("BOT_TOKEN", "")
    if not bot_token:
        raise ValueError("BOT_TOKEN не задан в .env")

    admin_id_str = os.getenv("ADMIN_TG_ID", "")
    if not admin_id_str:
        raise ValueError("ADMIN_TG_ID не задан в .env")

    editor_id_str = os.getenv("EDITOR_TG_ID", "")
    editor_id = int(editor_id_str) if editor_id_str.strip() else None

    is_test_str = os.getenv("ROBOKASSA_IS_TEST", "true").strip().lower()
    is_test = is_test_str in ("1", "true", "yes", "on")

    # Список tg_id админов: "123456789,987654321" → [123456789, 987654321]
    admin_tg_ids_raw = os.getenv("ADMIN_TG_IDS", "").strip()
    if admin_tg_ids_raw:
        admin_tg_ids = [int(x.strip()) for x in admin_tg_ids_raw.split(",") if x.strip().isdigit()]
    else:
        # Если ADMIN_TG_IDS не задан, используем основной ADMIN_TG_ID
        admin_tg_ids = [int(admin_id_str)] if admin_id_str else None

    webhook_host = os.getenv("WEBHOOK_HOST", "").rstrip("/")
    public_url = (os.getenv("PUBLIC_URL", "") or webhook_host or "https://example.com").rstrip("/")
    support_email = os.getenv("SUPPORT_EMAIL", "support@example.com").strip() or "support@example.com"
    bot_username = os.getenv("BOT_USERNAME", "your_study_bot").strip().lstrip("@") or "your_study_bot"

    session_secret = os.getenv("SESSION_SECRET", "").strip() or secrets.token_urlsafe(32)
    csrf_secret = os.getenv("CSRF_SECRET", "").strip() or secrets.token_urlsafe(32)

    return Settings(
        BOT_TOKEN=bot_token,
        ADMIN_TG_ID=int(admin_id_str),
        EDITOR_TG_ID=editor_id,
        WEBHOOK_HOST=webhook_host,
        # White-label
        BRAND_NAME=os.getenv("BRAND_NAME", "StudyFlow").strip() or "StudyFlow",
        BOT_USERNAME=bot_username,
        PUBLIC_URL=public_url,
        SUPPORT_EMAIL=support_email,
        OWNER_NAME=os.getenv("OWNER_NAME", "Иванов Иван Иванович").strip() or "Иванов Иван Иванович",
        OWNER_INN=os.getenv("OWNER_INN", "000000000000").strip() or "000000000000",
        OWNER_CITY=os.getenv("OWNER_CITY", "Москва").strip() or "Москва",
        WATERMARK_TEXT=os.getenv("WATERMARK_TEXT", "ОПЛАТИТЕ ДЛЯ ПОЛУЧЕНИЯ").strip() or "ОПЛАТИТЕ ДЛЯ ПОЛУЧЕНИЯ",
        # Робокасса
        ROBOKASSA_LOGIN=os.getenv("ROBOKASSA_LOGIN", ""),
        ROBOKASSA_PASSWORD_1=os.getenv("ROBOKASSA_PASSWORD_1", ""),
        ROBOKASSA_PASSWORD_2=os.getenv("ROBOKASSA_PASSWORD_2", ""),
        ROBOKASSA_TEST_PASSWORD_1=os.getenv("ROBOKASSA_TEST_PASSWORD_1", ""),
        ROBOKASSA_TEST_PASSWORD_2=os.getenv("ROBOKASSA_TEST_PASSWORD_2", ""),
        ROBOKASSA_IS_TEST=is_test,
        ROBOKASSA_HASH_ALGO=os.getenv("ROBOKASSA_HASH_ALGO", "sha256").lower(),
        ROBOKASSA_FALLBACK_EMAIL=os.getenv("ROBOKASSA_FALLBACK_EMAIL", support_email),
        # Старые ЮКасса-поля (опционально)
        YUKASSA_SHOP_ID=os.getenv("YUKASSA_SHOP_ID", ""),
        YUKASSA_SECRET_KEY=os.getenv("YUKASSA_SECRET_KEY", ""),
        YUKASSA_FALLBACK_EMAIL=os.getenv("YUKASSA_FALLBACK_EMAIL", ""),
        # Веб-админка
        ADMIN_TG_IDS=admin_tg_ids,
        SESSION_SECRET=session_secret,
        SESSION_TTL_HOURS=int(os.getenv("SESSION_TTL_HOURS", "24")),
        CSRF_SECRET=csrf_secret,
        ADMIN_WEB_PORT=int(os.getenv("ADMIN_WEB_PORT", "8081")),
    )


# Синглтон настроек
settings = _load_settings()
