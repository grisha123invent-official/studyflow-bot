"""
Отправка Telegram-уведомлений из веб-процесса.

Использует короткоживущий aiogram.Bot (тот же BOT_TOKEN, что и у основного бота,
но без polling). После каждой отправки сессия закрывается. Это безопасно для
параллельной работы с самим ботом — Telegram API ratelimit относится к токену,
а не к конкретной сессии, и API легко выдерживает несколько одновременных
коротких подключений.

При TelegramForbiddenError (пользователь заблокировал бот) — помечаем юзера
is_banned=True через db.crud.ban_user.
"""
import logging
import os
from typing import Optional

from aiogram import Bot
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.exceptions import TelegramForbiddenError, TelegramBadRequest
from aiogram.types import FSInputFile

from config import settings
from db.crud import ban_user
from db.session import AsyncSessionLocal

logger = logging.getLogger("admin.notifications")


def _make_bot() -> Bot:
    if not settings.BOT_TOKEN:
        raise RuntimeError("BOT_TOKEN не задан — нельзя отправлять уведомления")
    return Bot(
        token=settings.BOT_TOKEN,
        default=DefaultBotProperties(parse_mode=ParseMode.HTML),
    )


async def notify_telegram(
    tg_id: int,
    text: str,
    parse_mode: str = "HTML",
) -> bool:
    """
    Шлёт текстовое сообщение пользователю в Telegram.
    Возвращает True при успехе, False — при любой ошибке.

    При блокировке бота — помечает юзера is_banned.
    """
    bot = _make_bot()
    try:
        await bot.send_message(chat_id=tg_id, text=text, parse_mode=parse_mode)
        return True
    except TelegramForbiddenError:
        logger.warning(f"Пользователь {tg_id} заблокировал бот — помечаем is_banned")
        try:
            async with AsyncSessionLocal() as session:
                await ban_user(session, tg_id)
        except Exception as e:
            logger.error(f"Не удалось пометить is_banned для {tg_id}: {e}")
        return False
    except TelegramBadRequest as e:
        logger.error(f"BadRequest при отправке {tg_id}: {e}")
        return False
    except Exception as e:
        logger.error(f"Не удалось отправить уведомление {tg_id}: {e}")
        return False
    finally:
        await bot.session.close()


async def send_document_telegram(
    tg_id: int,
    file_path: str,
    caption: Optional[str] = None,
    filename: Optional[str] = None,
) -> bool:
    """
    Шлёт файл (документ) пользователю в Telegram.
    file_path — абсолютный или относительный путь.
    filename — имя файла, под которым он отобразится у получателя.
    """
    if not os.path.exists(file_path):
        logger.error(f"Файл не найден: {file_path}")
        return False

    bot = _make_bot()
    try:
        doc = FSInputFile(file_path, filename=filename) if filename else FSInputFile(file_path)
        await bot.send_document(chat_id=tg_id, document=doc, caption=caption)
        return True
    except TelegramForbiddenError:
        logger.warning(f"Пользователь {tg_id} заблокировал бот — помечаем is_banned")
        try:
            async with AsyncSessionLocal() as session:
                await ban_user(session, tg_id)
        except Exception as e:
            logger.error(f"Не удалось пометить is_banned для {tg_id}: {e}")
        return False
    except Exception as e:
        logger.error(f"Не удалось отправить документ {tg_id}: {e}")
        return False
    finally:
        await bot.session.close()
