"""
Тесты CRUD-операций и моделей БД.
Используют отдельную in-memory SQLite БД.
"""
import asyncio
import pytest
from sqlalchemy.ext.asyncio import create_async_engine, AsyncSession, async_sessionmaker
from sqlalchemy.orm import DeclarativeBase

from db.session import Base
from db.models import (
    User, Order, File, Payment, ReferralBonus, WithdrawalRequest,
    UserRole, OrderStatus, FileType, PaymentStatus, BonusType, WithdrawalStatus,
)
from db.crud import (
    get_or_create_user, get_user_by_tg_id, create_order, get_order,
    update_order_status, update_order_price, get_user_orders, get_all_orders,
    get_orders_by_status, save_file, get_file, create_payment, update_payment_status,
    set_referrer, add_referral_bonus, get_referral_stats, count_paid_orders,
    create_withdrawal_request, get_pending_withdrawals, count_pending_withdrawals,
    update_withdrawal_status, get_admin_analytics, get_all_referral_debtors,
)


# ─── Фикстуры ────────────────────────────────────────────────────────────────

@pytest.fixture
def event_loop():
    loop = asyncio.new_event_loop()
    yield loop
    loop.close()


@pytest.fixture
async def db_session():
    """Создаёт изолированную in-memory БД для каждого теста."""
    engine = create_async_engine("sqlite+aiosqlite:///:memory:", echo=False)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    async with factory() as session:
        yield session

    await engine.dispose()


@pytest.fixture
async def admin_user(db_session):
    return await get_or_create_user(db_session, tg_id=100, username="admin", full_name="Admin",
                                    admin_tg_id=100)


@pytest.fixture
async def editor_user(db_session):
    return await get_or_create_user(db_session, tg_id=200, username="editor", full_name="Editor",
                                    editor_tg_id=200)


@pytest.fixture
async def student_user(db_session):
    return await get_or_create_user(db_session, tg_id=300, username="student", full_name="Student")


@pytest.fixture
async def sample_order(db_session, student_user):
    return await create_order(
        db_session, user_id=student_user.id,
        work_type="referat", topic="Тест", subject="Физика",
        pages=10, deadline="01.06.2026", price=500.0,
    )


# ─── Тесты пользователей ────────────────────────────────────────────────────

class TestUsers:
    async def test_create_student(self, db_session):
        user = await get_or_create_user(db_session, tg_id=999, username="u1")
        assert user.role == UserRole.STUDENT.value
        assert user.tg_id == 999

    async def test_create_admin_by_tg_id(self, db_session):
        user = await get_or_create_user(db_session, tg_id=111, admin_tg_id=111)
        assert user.role == UserRole.ADMIN.value

    async def test_create_editor_by_tg_id(self, db_session):
        user = await get_or_create_user(db_session, tg_id=222, editor_tg_id=222)
        assert user.role == UserRole.EDITOR.value

    async def test_idempotent_creation(self, db_session):
        u1 = await get_or_create_user(db_session, tg_id=500, username="alice")
        u2 = await get_or_create_user(db_session, tg_id=500, username="alice_updated")
        assert u1.id == u2.id
        assert u2.username == "alice_updated"

    async def test_get_by_tg_id(self, db_session, student_user):
        found = await get_user_by_tg_id(db_session, student_user.tg_id)
        assert found is not None
        assert found.id == student_user.id

    async def test_get_nonexistent(self, db_session):
        result = await get_user_by_tg_id(db_session, tg_id=9999999)
        assert result is None

    async def test_no_self_admin_override(self, db_session):
        """Если tg_id не совпадает с admin_tg_id — обычный студент"""
        user = await get_or_create_user(db_session, tg_id=600, admin_tg_id=999)
        assert user.role == UserRole.STUDENT.value


# ─── Тесты заказов ──────────────────────────────────────────────────────────

class TestOrders:
    async def test_create_order(self, db_session, student_user):
        order = await create_order(
            db_session, user_id=student_user.id,
            work_type="kursovaya", topic="Экономика труда",
            subject="Экономика", pages=25, deadline="15.06.2026",
        )
        assert order.id is not None
        assert order.status == OrderStatus.PENDING.value
        assert order.topic == "Экономика труда"

    async def test_get_order(self, db_session, sample_order):
        found = await get_order(db_session, sample_order.id)
        assert found is not None
        assert found.id == sample_order.id

    async def test_get_nonexistent_order(self, db_session):
        result = await get_order(db_session, order_id=99999)
        assert result is None

    async def test_update_status(self, db_session, sample_order):
        await update_order_status(db_session, sample_order.id, OrderStatus.IN_WORK)
        order = await get_order(db_session, sample_order.id)
        assert order.status == OrderStatus.IN_WORK.value

    async def test_update_price(self, db_session, sample_order):
        await update_order_price(db_session, sample_order.id, 1200.0)
        order = await get_order(db_session, sample_order.id)
        assert order.price == 1200.0

    async def test_get_user_orders(self, db_session, student_user):
        for i in range(3):
            await create_order(db_session, user_id=student_user.id,
                               work_type="referat", topic=f"Тема {i}",
                               subject="Химия", pages=5, deadline="01.07.2026")
        orders = await get_user_orders(db_session, student_user.id)
        assert len(orders) >= 3

    async def test_get_orders_by_status(self, db_session, student_user):
        o1 = await create_order(db_session, user_id=student_user.id,
                                work_type="referat", topic="A", subject="B",
                                pages=5, deadline="01.07.2026")
        await update_order_status(db_session, o1.id, OrderStatus.EDITING)
        orders = await get_orders_by_status(db_session, OrderStatus.EDITING)
        assert any(o.id == o1.id for o in orders)

    async def test_get_all_orders(self, db_session, sample_order):
        orders = await get_all_orders(db_session)
        assert len(orders) >= 1

    async def test_status_flow(self, db_session, sample_order):
        """Полный цикл статусов заказа"""
        flow = [
            OrderStatus.IN_WORK, OrderStatus.EDITING,
            OrderStatus.PREVIEW_SENT, OrderStatus.AWAITING_PAYMENT, OrderStatus.DONE,
        ]
        for status in flow:
            await update_order_status(db_session, sample_order.id, status)
            order = await get_order(db_session, sample_order.id)
            assert order.status == status.value


# ─── Тесты файлов ───────────────────────────────────────────────────────────

class TestFiles:
    async def test_save_and_get_file(self, db_session, sample_order):
        f = await save_file(db_session, order_id=sample_order.id,
                            file_type=FileType.DRAFT,
                            filename="draft.docx", local_path="/tmp/draft.docx")
        assert f.id is not None

        found = await get_file(db_session, sample_order.id, FileType.DRAFT)
        assert found is not None
        assert found.filename == "draft.docx"

    async def test_get_file_returns_latest(self, db_session, sample_order):
        """get_file возвращает последний файл"""
        await save_file(db_session, sample_order.id, FileType.FINAL,
                        filename="v1.docx", local_path="/tmp/v1.docx")
        await save_file(db_session, sample_order.id, FileType.FINAL,
                        filename="v2.docx", local_path="/tmp/v2.docx")
        latest = await get_file(db_session, sample_order.id, FileType.FINAL)
        assert latest.filename == "v2.docx"

    async def test_get_nonexistent_file(self, db_session, sample_order):
        result = await get_file(db_session, sample_order.id, FileType.PREVIEW)
        assert result is None


# ─── Тесты платежей ─────────────────────────────────────────────────────────

class TestPayments:
    async def test_create_payment(self, db_session, sample_order):
        p = await create_payment(db_session, order_id=sample_order.id,
                                 amount=500.0, payment_id="yk_test_123",
                                 confirmation_url="https://pay.example.com")
        assert p.id is not None
        assert p.status == PaymentStatus.PENDING.value

    async def test_update_payment_status(self, db_session, sample_order):
        await create_payment(db_session, order_id=sample_order.id,
                             amount=500.0, payment_id="yk_test_456")
        updated = await update_payment_status(db_session, "yk_test_456", PaymentStatus.PAID)
        assert updated is not None
        assert updated.status == PaymentStatus.PAID.value
        assert updated.paid_at is not None

    async def test_update_nonexistent_payment(self, db_session):
        result = await update_payment_status(db_session, "nonexistent_id", PaymentStatus.PAID)
        assert result is None


# ─── Тесты реферальной системы ───────────────────────────────────────────────

class TestReferrals:
    async def test_set_referrer(self, db_session):
        referrer = await get_or_create_user(db_session, tg_id=700, username="ref")
        invited = await get_or_create_user(db_session, tg_id=701, username="inv")
        result = await set_referrer(db_session, user_id=invited.id,
                                    referrer_tg_id=referrer.tg_id)
        assert result is True

    async def test_no_self_referral(self, db_session):
        user = await get_or_create_user(db_session, tg_id=710, username="solo")
        result = await set_referrer(db_session, user_id=user.id,
                                    referrer_tg_id=user.tg_id)
        assert result is False

    async def test_referrer_not_set_twice(self, db_session):
        ref1 = await get_or_create_user(db_session, tg_id=720)
        ref2 = await get_or_create_user(db_session, tg_id=721)
        inv = await get_or_create_user(db_session, tg_id=722)
        await set_referrer(db_session, inv.id, ref1.tg_id)
        result = await set_referrer(db_session, inv.id, ref2.tg_id)
        assert result is False

    async def test_add_referral_bonus_balance(self, db_session):
        referrer = await get_or_create_user(db_session, tg_id=730)
        invited = await get_or_create_user(db_session, tg_id=731)
        order = await create_order(db_session, user_id=invited.id,
                                   work_type="referat", topic="T", subject="S",
                                   pages=5, deadline="01.07.2026")
        await add_referral_bonus(db_session, referrer_id=referrer.id,
                                 referred_id=invited.id, order_id=order.id,
                                 bonus_type=BonusType.FIRST_ORDER, amount=200.0)
        stats = await get_referral_stats(db_session, referrer.id)
        assert stats["balance"] == 200.0
        assert stats["total_earned"] == 200.0

    async def test_referral_stats_counts(self, db_session):
        ref = await get_or_create_user(db_session, tg_id=740)
        for i in range(3):
            inv = await get_or_create_user(db_session, tg_id=741 + i)
            await set_referrer(db_session, inv.id, ref.tg_id)
        stats = await get_referral_stats(db_session, ref.id)
        assert stats["referrals_count"] == 3

    async def test_count_paid_orders(self, db_session, student_user):
        o1 = await create_order(db_session, user_id=student_user.id,
                                work_type="referat", topic="X", subject="Y",
                                pages=5, deadline="01.07.2026")
        o2 = await create_order(db_session, user_id=student_user.id,
                                work_type="referat", topic="X2", subject="Y2",
                                pages=5, deadline="01.07.2026")
        await update_order_status(db_session, o1.id, OrderStatus.DONE)
        count = await count_paid_orders(db_session, student_user.id)
        assert count == 1


# ─── Тесты вывода средств ────────────────────────────────────────────────────

class TestWithdrawals:
    async def test_create_withdrawal_deducts_balance(self, db_session):
        user = await get_or_create_user(db_session, tg_id=800)
        order = await create_order(db_session, user_id=user.id,
                                   work_type="referat", topic="W", subject="S",
                                   pages=5, deadline="01.07.2026")
        await add_referral_bonus(db_session, referrer_id=user.id,
                                 referred_id=user.id, order_id=order.id,
                                 bonus_type=BonusType.FIRST_ORDER, amount=500.0)
        req = await create_withdrawal_request(db_session, user_id=user.id,
                                              amount=300.0, card_details="4111111111111111")
        assert req.status == WithdrawalStatus.PENDING.value
        stats = await get_referral_stats(db_session, user.id)
        assert stats["balance"] == 200.0

    async def test_reject_withdrawal_restores_balance(self, db_session):
        user = await get_or_create_user(db_session, tg_id=810)
        order = await create_order(db_session, user_id=user.id,
                                   work_type="referat", topic="W", subject="S",
                                   pages=5, deadline="01.07.2026")
        await add_referral_bonus(db_session, referrer_id=user.id,
                                 referred_id=user.id, order_id=order.id,
                                 bonus_type=BonusType.FIRST_ORDER, amount=500.0)
        req = await create_withdrawal_request(db_session, user_id=user.id,
                                              amount=500.0, card_details="4111111111111111")
        await update_withdrawal_status(db_session, req.id, WithdrawalStatus.REJECTED)
        stats = await get_referral_stats(db_session, user.id)
        assert stats["balance"] == 500.0

    async def test_get_pending_withdrawals(self, db_session):
        user = await get_or_create_user(db_session, tg_id=820)
        order = await create_order(db_session, user_id=user.id,
                                   work_type="referat", topic="W", subject="S",
                                   pages=5, deadline="01.07.2026")
        await add_referral_bonus(db_session, referrer_id=user.id,
                                 referred_id=user.id, order_id=order.id,
                                 bonus_type=BonusType.FIRST_ORDER, amount=300.0)
        await create_withdrawal_request(db_session, user_id=user.id,
                                        amount=300.0, card_details="4111111111111111")
        pending = await get_pending_withdrawals(db_session)
        count = await count_pending_withdrawals(db_session)
        assert len(pending) >= 1
        assert count >= 1


# ─── Тест аналитики ─────────────────────────────────────────────────────────

class TestAnalytics:
    async def test_analytics_runs_without_error(self, db_session, sample_order):
        stats = await get_admin_analytics(db_session)
        assert "total_users" in stats
        assert "total_orders" in stats
        assert "total_revenue" in stats
        assert "orders_by_status" in stats
        assert "top_referrers" in stats
        assert stats["total_orders"] >= 1

    async def test_referral_debtors(self, db_session):
        user = await get_or_create_user(db_session, tg_id=900)
        order = await create_order(db_session, user_id=user.id,
                                   work_type="referat", topic="D", subject="S",
                                   pages=5, deadline="01.07.2026")
        await add_referral_bonus(db_session, referrer_id=user.id,
                                 referred_id=user.id, order_id=order.id,
                                 bonus_type=BonusType.FIRST_ORDER, amount=200.0)
        debtors = await get_all_referral_debtors(db_session)
        assert any(d["user"].id == user.id for d in debtors)
        debtor = next(d for d in debtors if d["user"].id == user.id)
        assert debtor["balance"] == 200.0
