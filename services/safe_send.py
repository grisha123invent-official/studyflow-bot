"""
Безопасная отправка сообщений в Telegram.

Если пользователь заблокировал бота (TelegramForbiddenError) — помечаем его
как is_banned в БД, чтобы не пытаться слать ему сообщения дальше.
"""
import logging
from typing import Any, Optional
from aiogram import Bot
from aiogram.exceptions import TelegramForbiddenError, TelegramBadRequest

from db.crud import ban_user
from db.session import AsyncSessionLocal

logger = logging.getLogger(__name__)


async def safe_send_message(
    bot: Bot,
    chat_id: int,
    text: str,
    **kwargs: Any,
) -> bool:
    """
    Безопасно отправляет сообщение. Возвращает True если получилось.

    При TelegramForbiddenError (пользователь заблокировал бот / удалил аккаунт)
    помечает юзера is_banned=True. Другие ошибки — логируются.
    """
    try:
        await bot.send_message(chat_id=chat_id, text=text, **kwargs)
        return True
    except TelegramForbiddenError:
        logger.warning(f"Пользователь {chat_id} заблокировал бот, помечаем is_banned")
        async with AsyncSessionLocal() as session:
            await ban_user(session, chat_id)
        return False
    except TelegramBadRequest as e:
        logger.error(f"Bad request при отправке {chat_id}: {e}")
        return False
    except Exception as e:
        logger.error(f"Не удалось отправить сообщение {chat_id}: {e}")
        return False


async def safe_send_document(
    bot: Bot,
    chat_id: int,
    document: Any,
    **kwargs: Any,
) -> bool:
    """Аналогично safe_send_message, но для документов."""
    try:
        await bot.send_document(chat_id=chat_id, document=document, **kwargs)
        return True
    except TelegramForbiddenError:
        logger.warning(f"Пользователь {chat_id} заблокировал бот, помечаем is_banned")
        async with AsyncSessionLocal() as session:
            await ban_user(session, chat_id)
        return False
    except Exception as e:
        logger.error(f"Не удалось отправить документ {chat_id}: {e}")
        return False
