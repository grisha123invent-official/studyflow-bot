"""
Авторизация админ-панели через Telegram OTP.

Поток:
1. Админ вводит свой Telegram ID на сайте
2. Сервер проверяет, что ID в whitelist (settings.ADMIN_TG_IDS)
3. Генерируется 6-значный код, его SHA-256 hash хранится in-memory (TTL 5 мин)
4. Через aiogram.Bot код отправляется в Telegram админа
5. Админ вводит код на сайте
6. Сервер сравнивает hash → выдаёт signed cookie (itsdangerous, TTL 24ч)

Безопасность:
- Коды cryptographically random (secrets.token_hex)
- Хранится только hash кода (если память дампится — кода не восстановишь)
- Sessions подписаны секретом из .env, при подмене → 401
- На каждую попытку ввода кода — rate-limit
"""
import hashlib
import logging
import secrets
import time
from dataclasses import dataclass
from typing import Optional

from itsdangerous import URLSafeTimedSerializer, BadSignature, SignatureExpired

from config import settings

logger = logging.getLogger("admin.auth")


# ─── In-memory store для OTP кодов ──────────────────────────────────────────
# Структура: {tg_id: PendingOTP}
# In-memory приемлемо для одного админа и одного процесса uvicorn (single worker).
# Если масштабироваться — заменить на Redis.

@dataclass
class PendingOTP:
    code_hash: str         # SHA-256 hex от 6-значного кода
    expires_at: float      # unix timestamp
    attempts: int = 0      # счётчик неверных попыток
    created_at: float = 0  # для rate-limit генерации новых кодов


# Кодами управляем сами — без внешнего store
_otp_store: dict[int, PendingOTP] = {}

# Параметры
OTP_TTL_SECONDS = 5 * 60           # 5 минут
OTP_MAX_ATTEMPTS = 5               # максимум попыток ввода для одного кода
OTP_REGEN_COOLDOWN_SECONDS = 30    # минимум 30 секунд между запросами нового кода


def _hash_code(code: str) -> str:
    """SHA-256 hash от кода (для безопасного хранения)."""
    return hashlib.sha256(code.encode("utf-8")).hexdigest()


def _cleanup_expired() -> None:
    """Удаляет просроченные OTP из памяти."""
    now = time.time()
    expired = [tg_id for tg_id, otp in _otp_store.items() if otp.expires_at < now]
    for tg_id in expired:
        del _otp_store[tg_id]


def is_admin(tg_id: int) -> bool:
    """Проверяет, что tg_id входит в whitelist администраторов."""
    allowed = settings.ADMIN_TG_IDS or []
    return tg_id in allowed


def can_request_new_code(tg_id: int) -> bool:
    """Проверяет cooldown: нельзя запрашивать новый код чаще раза в 30 секунд."""
    existing = _otp_store.get(tg_id)
    if existing is None:
        return True
    return (time.time() - existing.created_at) >= OTP_REGEN_COOLDOWN_SECONDS


def generate_otp(tg_id: int) -> str:
    """
    Генерирует 6-значный код, сохраняет его hash, возвращает САМ код
    (для отправки в Telegram). После этой функции код в памяти НЕ хранится —
    только его hash.
    """
    _cleanup_expired()
    # Cryptographically secure 6-значный код (000000 — 999999)
    code = f"{secrets.randbelow(1_000_000):06d}"
    now = time.time()
    _otp_store[tg_id] = PendingOTP(
        code_hash=_hash_code(code),
        expires_at=now + OTP_TTL_SECONDS,
        attempts=0,
        created_at=now,
    )
    logger.info(f"OTP generated for tg_id={tg_id}, expires in {OTP_TTL_SECONDS}s")
    return code


def verify_otp(tg_id: int, code: str) -> tuple[bool, str]:
    """
    Проверяет введённый код.
    Возвращает (success, error_message).
    """
    _cleanup_expired()
    otp = _otp_store.get(tg_id)
    if otp is None:
        return False, "Код истёк или не запрашивался. Запросите новый."
    if otp.expires_at < time.time():
        del _otp_store[tg_id]
        return False, "Код истёк. Запросите новый."
    if otp.attempts >= OTP_MAX_ATTEMPTS:
        del _otp_store[tg_id]
        return False, "Превышено количество попыток. Запросите новый код."

    # Сравнение в постоянном времени (защита от timing attack)
    is_valid = secrets.compare_digest(otp.code_hash, _hash_code(code.strip()))
    if not is_valid:
        otp.attempts += 1
        remaining = OTP_MAX_ATTEMPTS - otp.attempts
        return False, f"Неверный код. Осталось попыток: {remaining}"

    # Успех — удаляем код, он одноразовый
    del _otp_store[tg_id]
    return True, ""


def invalidate_otp(tg_id: int) -> None:
    """Удаляет код из памяти (для logout или отмены)."""
    _otp_store.pop(tg_id, None)


# ─── Cookie sessions (itsdangerous) ─────────────────────────────────────────

def _get_signer() -> URLSafeTimedSerializer:
    if not settings.SESSION_SECRET:
        raise RuntimeError("SESSION_SECRET не задан в .env — нельзя подписывать сессии")
    return URLSafeTimedSerializer(settings.SESSION_SECRET, salt="pishu24-admin-session")


def create_session_token(tg_id: int) -> str:
    """Создаёт подписанный токен сессии."""
    return _get_signer().dumps({"tg_id": tg_id})


def verify_session_token(token: str) -> Optional[int]:
    """
    Проверяет токен сессии. Возвращает tg_id или None.
    Истёкшие/повреждённые токены отвергаются.
    """
    if not token:
        return None
    try:
        max_age = settings.SESSION_TTL_HOURS * 3600
        data = _get_signer().loads(token, max_age=max_age)
        tg_id = data.get("tg_id")
        if not isinstance(tg_id, int):
            return None
        # Дополнительная проверка: tg_id всё ещё в whitelist (могли убрать админа)
        if not is_admin(tg_id):
            return None
        return tg_id
    except (BadSignature, SignatureExpired):
        return None
    except Exception as e:
        logger.warning(f"Session verification error: {e}")
        return None


# ─── Отправка кода через Telegram-бот ────────────────────────────────────────

async def send_otp_via_telegram(tg_id: int, code: str) -> bool:
    """
    Отправляет код пользователю через Telegram-бот.
    Использует тот же BOT_TOKEN, что и основной бот, но через отдельный экземпляр
    aiogram.Bot — без polling.

    Возвращает True при успехе, False при ошибке.
    """
    from aiogram import Bot
    from aiogram.client.default import DefaultBotProperties
    from aiogram.enums import ParseMode

    if not settings.BOT_TOKEN:
        logger.error("BOT_TOKEN не задан — нельзя отправить OTP")
        return False

    bot = Bot(
        token=settings.BOT_TOKEN,
        default=DefaultBotProperties(parse_mode=ParseMode.HTML),
    )
    try:
        await bot.send_message(
            chat_id=tg_id,
            text=(
                f"🔐 <b>Код входа в админ-панель {settings.BRAND_NAME}</b>\n\n"
                f"<code>{code}</code>\n\n"
                f"Действителен 5 минут. Если вы не запрашивали — игнорируйте."
            ),
            parse_mode="HTML",
        )
        return True
    except Exception as e:
        logger.error(f"Failed to send OTP to tg_id={tg_id}: {e}")
        return False
    finally:
        await bot.session.close()
