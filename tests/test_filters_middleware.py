"""
Тесты RoleFilter, RoleMiddleware и клавиатур.
"""
import pytest
from unittest.mock import AsyncMock, MagicMock, patch
from db.models import UserRole


# ─── Тесты RoleFilter ────────────────────────────────────────────────────────

class TestRoleFilter:
    @pytest.fixture
    def make_user(self):
        def _make(role_value: str):
            u = MagicMock()
            u.role = role_value
            return u
        return _make

    @pytest.fixture
    def make_enum_user(self):
        """Пользователь с role как enum-объект (как возвращает SQLAlchemy)."""
        def _make(role_enum):
            u = MagicMock()
            u.role = role_enum
            return u
        return _make

    async def test_editor_string_passes(self, make_user):
        from bot.filters import RoleFilter
        f = RoleFilter("editor")
        assert await f(MagicMock(), user=make_user("editor")) is True

    async def test_editor_enum_passes(self, make_enum_user):
        from bot.filters import RoleFilter
        f = RoleFilter("editor")
        assert await f(MagicMock(), user=make_enum_user(UserRole.EDITOR)) is True

    async def test_admin_passes(self, make_user):
        from bot.filters import RoleFilter
        f = RoleFilter("admin")
        assert await f(MagicMock(), user=make_user("admin")) is True

    async def test_wrong_role_fails(self, make_user):
        from bot.filters import RoleFilter
        f = RoleFilter("editor")
        assert await f(MagicMock(), user=make_user("student")) is False

    async def test_none_user_fails(self):
        from bot.filters import RoleFilter
        f = RoleFilter("editor")
        assert await f(MagicMock(), user=None) is False

    async def test_admin_enum_passes(self, make_enum_user):
        from bot.filters import RoleFilter
        f = RoleFilter("admin")
        assert await f(MagicMock(), user=make_enum_user(UserRole.ADMIN)) is True

    async def test_student_enum_passes(self, make_enum_user):
        from bot.filters import RoleFilter
        f = RoleFilter("student")
        assert await f(MagicMock(), user=make_enum_user(UserRole.STUDENT)) is True


# ─── Тесты клавиатур ────────────────────────────────────────────────────────

class TestKeyboards:
    def test_admin_main_kb_has_required_buttons(self):
        from bot.keyboards.admin_kb import admin_main_kb
        kb = admin_main_kb()
        texts = [btn.text for row in kb.keyboard for btn in row]
        assert "📋 Заявки" in texts
        assert "💸 Выводы" in texts
        assert "📊 Аналитика" in texts
        assert "📂 Все заказы" in texts
        assert "🔗 Рефералы" in texts

    def test_editor_main_kb_has_zadaniya(self):
        from bot.keyboards.admin_kb import editor_main_kb
        kb = editor_main_kb()
        texts = [btn.text for row in kb.keyboard for btn in row]
        assert "📋 Мои задания" in texts

    def test_admin_order_kb_has_actions(self):
        from bot.keyboards.admin_kb import admin_order_kb
        kb = admin_order_kb(order_id=42)
        buttons = [btn for row in kb.inline_keyboard for btn in row]
        callbacks = [b.callback_data for b in buttons]
        assert "accept_42" in callbacks
        assert "reject_42" in callbacks
        assert "change_price_42" in callbacks

    def test_editor_task_kb_callback(self):
        from bot.keyboards.admin_kb import editor_task_kb
        kb = editor_task_kb(order_id=7)
        buttons = [btn for row in kb.inline_keyboard for btn in row]
        assert any("editor_start_7" in b.callback_data for b in buttons)

    def test_editor_preview_kb_buttons(self):
        from bot.keyboards.admin_kb import editor_preview_kb
        kb = editor_preview_kb(order_id=5, variant=2)
        buttons = [btn for row in kb.inline_keyboard for btn in row]
        callbacks = [b.callback_data for b in buttons]
        assert "preview_approve_5_2" in callbacks
        assert "preview_regen_5_2" in callbacks

    def test_withdrawal_action_kb(self):
        from bot.keyboards.admin_kb import withdrawal_action_kb
        kb = withdrawal_action_kb(wd_id=99)
        buttons = [btn for row in kb.inline_keyboard for btn in row]
        callbacks = [b.callback_data for b in buttons]
        assert "approve_wd_99" in callbacks
        assert "reject_wd_99" in callbacks

    def test_student_main_menu_has_new_order(self):
        from bot.keyboards.student_kb import main_menu_kb
        kb = main_menu_kb("student")
        texts = [btn.text for row in kb.keyboard for btn in row]
        assert "📋 Новая заявка" in texts
        assert "📂 Мои заказы" in texts

    def test_editor_main_menu_has_zadaniya(self):
        from bot.keyboards.student_kb import main_menu_kb
        kb = main_menu_kb("editor")
        texts = [btn.text for row in kb.keyboard for btn in row]
        assert "📋 Мои задания" in texts

    def test_admin_orders_kb_skips_cancelled(self):
        from bot.keyboards.admin_kb import admin_orders_kb
        orders = []
        for i, status in enumerate(["PENDING", "CANCELLED", "IN_WORK"]):
            o = MagicMock()
            o.id = i + 1
            o.status = status
            o.work_type = "referat"
            o.topic = f"Тема {i}"
            o.deadline = "01.07"
            orders.append(o)
        kb = admin_orders_kb(orders)
        callbacks = [btn.callback_data for row in kb.inline_keyboard for btn in row]
        # #2 CANCELLED не должен быть
        assert not any("view_order_2" in c for c in callbacks)
        assert any("view_order_1" in c for c in callbacks)
        assert any("view_order_3" in c for c in callbacks)

    def test_admin_orders_kb_skips_refunded(self):
        from bot.keyboards.admin_kb import admin_orders_kb
        orders = []
        for i, status in enumerate(["PENDING", "REFUNDED", "DONE"]):
            o = MagicMock()
            o.id = i + 10
            o.status = status
            o.work_type = "referat"
            o.topic = f"Тема {i}"
            o.deadline = "01.07"
            orders.append(o)
        kb = admin_orders_kb(orders)
        callbacks = [btn.callback_data for row in kb.inline_keyboard for btn in row]
        assert not any("view_order_11" in c for c in callbacks)  # REFUNDED скрыт
        assert any("view_order_10" in c for c in callbacks)
        assert any("view_order_12" in c for c in callbacks)
