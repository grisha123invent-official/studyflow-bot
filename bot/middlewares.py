"""
Middleware для проверки роли пользователя.
При каждом обращении к боту:
- создаёт или обновляет пользователя в БД
- добавляет объект User в data['user']
- блокирует забаненных пользователей
"""
from typing import Any, Callable, Awaitable
from aiogram import BaseMiddleware
from aiogram.types import Message, CallbackQuery, TelegramObject

from config import settings
from db.session import AsyncSessionLocal
from db.crud import get_or_create_user


class RoleMiddleware(BaseMiddleware):
    """
    Middleware для идентификации и авторизации пользователей.
    Добавляет объект User в data['user'] для всех хендлеров.
    """

    async def __call__(
        self,
        handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: dict[str, Any],
    ) -> Any:
        # Получаем tg_id в зависимости от типа апдейта
        from_user = None
        if isinstance(event, Message):
            from_user = event.from_user
        elif isinstance(event, CallbackQuery):
            from_user = event.from_user

        if from_user is None:
            # Неизвестный тип апдейта — пропускаем без user
            return await handler(event, data)

        async with AsyncSessionLocal() as session:
            user = await get_or_create_user(
                session=session,
                tg_id=from_user.id,
                username=from_user.username,
                full_name=from_user.full_name,
                admin_tg_id=settings.ADMIN_TG_ID,
                editor_tg_id=settings.EDITOR_TG_ID,
            )

            # Проверяем бан
            if user.is_banned:
                if isinstance(event, Message):
                    await event.answer("🚫 Вы заблокированы. Обратитесь к администратору.")
                elif isinstance(event, CallbackQuery):
                    await event.answer("🚫 Вы заблокированы.", show_alert=True)
                return  # Прерываем цепочку

            # Передаём user в хендлеры через data
            data["user"] = user
            data["session"] = session

            return await handler(event, data)
