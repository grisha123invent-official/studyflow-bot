"""
Role checks для разных ролей админ/редактор.

Используется в `web/deps.py` (require_admin) и `web/deps_editor.py` (require_editor).
"""
from config import settings


def is_editor(tg_id: int) -> bool:
    """Проверяет, что tg_id — редактор (whitelist = [settings.EDITOR_TG_ID])."""
    editor_id = settings.EDITOR_TG_ID
    return editor_id is not None and tg_id == editor_id
