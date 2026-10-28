"""
Кастомные фильтры для aiogram 3.x.
"""
from aiogram.filters import BaseFilter
from aiogram.types import Message


class RoleFilter(BaseFilter):
    """Фильтр по роли пользователя (из middleware data['user'])."""

    def __init__(self, role: str) -> None:
        self.role = role

    async def __call__(self, message: Message, user=None) -> bool:  # type: ignore[override]
        if user is None:
            return False
        role_val = user.role.value if hasattr(user.role, "value") else user.role
        return role_val == self.role
