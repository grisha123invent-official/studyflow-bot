"""
Тесты критичных фиксов:
- Идемпотентность реферального бонуса (UNIQUE order_id)
- Атомарное списание баланса при выводе
- Receipt 54-ФЗ для самозанятого
"""
import asyncio
import pytest
from sqlalchemy.ext.asyncio import create_async_engine, AsyncSession, async_sessionmaker

from db.session import Base
from db.models import User, OrderStatus, BonusType
from db.crud import (
    get_or_create_user, create_order, update_order_status,
    add_referral_bonus, create_withdrawal_request, get_referral_stats,
    update_user_email,
)


@pytest.fixture
async def db_session():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:", echo=False)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    async with factory() as session:
        yield session
    await engine.dispose()


# ─── 1. Идемпотентность реферального бонуса ─────────────────────────────────

class TestReferralIdempotency:
    async def test_double_bonus_does_not_double_balance(self, db_session):
        """Повторный вызов add_referral_bonus не дублирует баланс"""
        ref = await get_or_create_user(db_session, tg_id=1001)
        inv = await get_or_create_user(db_session, tg_id=1002)
        order = await create_order(
            db_session, user_id=inv.id, work_type="referat",
            topic="T", subject="S", pages=5, deadline="01.07.2026", price=1000.0,
        )

        # Первое начисление
        b1 = await add_referral_bonus(
            db_session, referrer_id=ref.id, referred_id=inv.id,
            order_id=order.id, bonus_type=BonusType.FIRST_ORDER, amount=200.0,
        )
        # Повторный вызов — webhook-ретрай
        b2 = await add_referral_bonus(
            db_session, referrer_id=ref.id, referred_id=inv.id,
            order_id=order.id, bonus_type=BonusType.FIRST_ORDER, amount=200.0,
        )

        assert b1.id == b2.id, "Должен вернуть существующую запись, а не создать новую"

        stats = await get_referral_stats(db_session, ref.id)
        assert stats["balance"] == 200.0, "Баланс не должен удвоиться"
        assert stats["total_earned"] == 200.0

    async def test_different_orders_get_different_bonuses(self, db_session):
        """Бонус за разные заказы начисляется отдельно (UNIQUE на order_id)"""
        ref = await get_or_create_user(db_session, tg_id=1003)
        inv = await get_or_create_user(db_session, tg_id=1004)

        order1 = await create_order(
            db_session, user_id=inv.id, work_type="referat",
            topic="T1", subject="S", pages=5, deadline="01.07.2026", price=1000.0,
        )
        order2 = await create_order(
            db_session, user_id=inv.id, work_type="referat",
            topic="T2", subject="S", pages=5, deadline="01.07.2026", price=2000.0,
        )

        await add_referral_bonus(
            db_session, ref.id, inv.id, order1.id, BonusType.FIRST_ORDER, 200.0,
        )
        await add_referral_bonus(
            db_session, ref.id, inv.id, order2.id, BonusType.REPEAT, 100.0,
        )

        stats = await get_referral_stats(db_session, ref.id)
        assert stats["balance"] == 300.0


# ─── 2. Атомарное списание баланса ──────────────────────────────────────────

class TestAtomicWithdrawal:
    async def test_cannot_withdraw_more_than_balance(self, db_session):
        """Запрос вывода больше баланса бросает ValueError"""
        user = await get_or_create_user(db_session, tg_id=2001)
        order = await create_order(
            db_session, user_id=user.id, work_type="referat",
            topic="T", subject="S", pages=5, deadline="01.07.2026", price=1000.0,
        )
        await add_referral_bonus(
            db_session, user.id, user.id, order.id, BonusType.FIRST_ORDER, 500.0,
        )

        # Попытка вывести 1000 при балансе 500
        with pytest.raises(ValueError, match="Недостаточно"):
            await create_withdrawal_request(
                db_session, user_id=user.id, amount=1000.0,
                card_details="4111111111111111",
            )

        # Баланс не пострадал
        stats = await get_referral_stats(db_session, user.id)
        assert stats["balance"] == 500.0

    async def test_successful_withdrawal_deducts(self, db_session):
        """Успешный вывод корректно списывает с баланса"""
        user = await get_or_create_user(db_session, tg_id=2002)
        order = await create_order(
            db_session, user_id=user.id, work_type="referat",
            topic="T", subject="S", pages=5, deadline="01.07.2026", price=1000.0,
        )
        await add_referral_bonus(
            db_session, user.id, user.id, order.id, BonusType.FIRST_ORDER, 1000.0,
        )

        await create_withdrawal_request(
            db_session, user_id=user.id, amount=300.0,
            card_details="4111111111111111",
        )

        stats = await get_referral_stats(db_session, user.id)
        assert stats["balance"] == 700.0

    async def test_zero_balance_withdrawal_fails(self, db_session):
        """С нулевого баланса вывод невозможен"""
        user = await get_or_create_user(db_session, tg_id=2003)
        with pytest.raises(ValueError):
            await create_withdrawal_request(
                db_session, user_id=user.id, amount=100.0,
                card_details="4111111111111111",
            )


# ─── 3. Email пользователя ──────────────────────────────────────────────────

class TestUserEmail:
    async def test_update_email(self, db_session):
        user = await get_or_create_user(db_session, tg_id=3001)
        assert user.email is None
        await update_user_email(db_session, user.tg_id, "test@example.com")
        from db.crud import get_user_by_tg_id
        refreshed = await get_user_by_tg_id(db_session, user.tg_id)
        assert refreshed.email == "test@example.com"


# ─── 4. Receipt 54-ФЗ для Робокассы (НПД самозанятый) ──────────────────────

class TestReceipt54FZ:
    def test_receipt_items_structure(self):
        """Receipt содержит items с корректной структурой."""
        from services.payment import _build_receipt
        receipt = _build_receipt(amount=500.0, description="Консультация по заказу #1")
        assert "items" in receipt
        assert len(receipt["items"]) == 1
        item = receipt["items"][0]
        assert item["sum"] == 500.0
        assert item["quantity"] == 1
        assert item["tax"] == "none"  # без НДС (НПД)
        assert item["payment_method"] == "full_payment"
        assert item["payment_object"] == "service"

    def test_receipt_description_truncated(self):
        """Description обрезается до 128 символов (лимит Робокассы)."""
        from services.payment import _build_receipt
        receipt = _build_receipt(amount=100.0, description="A" * 200)
        assert len(receipt["items"][0]["name"]) == 128

    def test_receipt_sum_is_float(self):
        """Сумма в чеке — число с двумя знаками."""
        from services.payment import _build_receipt
        receipt = _build_receipt(amount=1234.5, description="X")
        assert receipt["items"][0]["sum"] == 1234.50


# ─── 5. Webhook идемпотентность (имитация) ──────────────────────────────────

class TestWebhookIdempotency:
    async def test_repeat_bonus_via_two_sessions(self, db_session):
        """Имитируем ретрай webhook в двух отдельных вызовах"""
        ref = await get_or_create_user(db_session, tg_id=5001)
        inv = await get_or_create_user(db_session, tg_id=5002)
        order = await create_order(
            db_session, user_id=inv.id, work_type="referat",
            topic="T", subject="S", pages=5, deadline="01.07.2026", price=2000.0,
        )

        # Webhook #1
        await add_referral_bonus(
            db_session, ref.id, inv.id, order.id, BonusType.FIRST_ORDER, 200.0,
        )
        # Webhook #2 (повторный)
        await add_referral_bonus(
            db_session, ref.id, inv.id, order.id, BonusType.FIRST_ORDER, 200.0,
        )
        # Webhook #3
        await add_referral_bonus(
            db_session, ref.id, inv.id, order.id, BonusType.FIRST_ORDER, 200.0,
        )

        stats = await get_referral_stats(db_session, ref.id)
        assert stats["balance"] == 200.0, "Только одно начисление, даже при 3 повторах"
