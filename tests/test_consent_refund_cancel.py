"""
Тесты новой функциональности:
- Согласие с офертой (consent)
- Отмена заявки студентом (только PENDING)
- Заявка на возврат (RefundRequest, идемпотентность, проверки)
"""
import pytest
from sqlalchemy.ext.asyncio import create_async_engine, AsyncSession, async_sessionmaker

from db.session import Base
from db.models import OrderStatus, BonusType, PaymentStatus, RefundStatus
from db.crud import (
    get_or_create_user, create_order, update_order_status, create_payment,
    update_payment_status,
    set_user_consent, cancel_order_by_student,
    get_user_paid_orders, create_refund_request,
    has_active_refund_request, get_refund_request, update_refund_status,
    get_pending_refund_requests, count_pending_refund_requests,
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


# ─── Согласие с офертой ─────────────────────────────────────────────────────

class TestConsent:
    async def test_new_user_has_no_consent(self, db_session):
        user = await get_or_create_user(db_session, tg_id=10001)
        assert user.consented_at is None

    async def test_set_consent_saves_timestamp(self, db_session):
        user = await get_or_create_user(db_session, tg_id=10002)
        assert user.consented_at is None
        await set_user_consent(db_session, user.tg_id)
        await db_session.refresh(user)
        assert user.consented_at is not None

    async def test_consent_idempotent(self, db_session):
        """Повторный вызов set_user_consent просто перезаписывает дату — не падает."""
        user = await get_or_create_user(db_session, tg_id=10003)
        await set_user_consent(db_session, user.tg_id)
        first = user.consented_at
        # Повторяем
        await set_user_consent(db_session, user.tg_id)
        # Не должно упасть
        await db_session.refresh(user)
        assert user.consented_at is not None


# ─── Отмена заявки студентом ─────────────────────────────────────────────────

class TestCancelOrder:
    async def test_cancel_pending_succeeds(self, db_session):
        user = await get_or_create_user(db_session, tg_id=20001)
        order = await create_order(
            db_session, user_id=user.id, work_type="referat",
            topic="T", subject="S", pages=5, deadline="01.07.2026",
        )
        success, msg = await cancel_order_by_student(db_session, order.id, user.id)
        assert success is True
        assert msg == ""
        # Проверяем что статус сменился
        from db.crud import get_order
        order_after = await get_order(db_session, order.id)
        status_val = order_after.status.value if hasattr(order_after.status, "value") else order_after.status
        assert status_val == OrderStatus.CANCELLED.value

    async def test_cannot_cancel_in_work_order(self, db_session):
        user = await get_or_create_user(db_session, tg_id=20002)
        order = await create_order(
            db_session, user_id=user.id, work_type="referat",
            topic="T", subject="S", pages=5, deadline="01.07.2026",
        )
        await update_order_status(db_session, order.id, OrderStatus.IN_WORK)
        success, msg = await cancel_order_by_student(db_session, order.id, user.id)
        assert success is False
        assert "Отмена возможна только до приёма" in msg

    async def test_cannot_cancel_editing_order(self, db_session):
        user = await get_or_create_user(db_session, tg_id=20003)
        order = await create_order(
            db_session, user_id=user.id, work_type="referat",
            topic="T", subject="S", pages=5, deadline="01.07.2026",
        )
        await update_order_status(db_session, order.id, OrderStatus.EDITING)
        success, _ = await cancel_order_by_student(db_session, order.id, user.id)
        assert success is False

    async def test_cannot_cancel_others_order(self, db_session):
        user_a = await get_or_create_user(db_session, tg_id=20010)
        user_b = await get_or_create_user(db_session, tg_id=20011)
        order = await create_order(
            db_session, user_id=user_a.id, work_type="referat",
            topic="T", subject="S", pages=5, deadline="01.07.2026",
        )
        success, msg = await cancel_order_by_student(db_session, order.id, user_b.id)
        assert success is False
        assert "Не ваш заказ" in msg

    async def test_cancel_nonexistent_order(self, db_session):
        user = await get_or_create_user(db_session, tg_id=20012)
        success, msg = await cancel_order_by_student(db_session, 999999, user.id)
        assert success is False
        assert "не найден" in msg.lower()


# ─── Возврат: заявки и обработка ────────────────────────────────────────────

async def _create_paid_order(db_session, user_id: int, amount: float = 1000.0):
    """Хелпер: создаёт заказ + PAID-платёж."""
    order = await create_order(
        db_session, user_id=user_id, work_type="referat",
        topic="T", subject="S", pages=10, deadline="01.07.2026", price=amount,
    )
    await update_order_status(db_session, order.id, OrderStatus.DONE)
    await create_payment(
        db_session, order_id=order.id, amount=amount,
        payment_id=f"yk_{order.id}", confirmation_url="https://example.com",
    )
    await update_payment_status(db_session, f"yk_{order.id}", PaymentStatus.PAID)
    return order


class TestRefundRequest:
    async def test_cannot_refund_unpaid_order(self, db_session):
        user = await get_or_create_user(db_session, tg_id=30001)
        order = await create_order(
            db_session, user_id=user.id, work_type="referat",
            topic="T", subject="S", pages=5, deadline="01.07.2026",
        )
        with pytest.raises(ValueError, match="оплаченных платежей"):
            await create_refund_request(
                db_session, user_id=user.id, order_id=order.id,
                reason="Передумал, нужен возврат",
            )

    async def test_create_refund_for_paid_order(self, db_session):
        user = await get_or_create_user(db_session, tg_id=30002)
        order = await _create_paid_order(db_session, user.id)
        req = await create_refund_request(
            db_session, user_id=user.id, order_id=order.id,
            reason="Работа меня не устроила",
        )
        assert req.id is not None
        status_val = req.status.value if hasattr(req.status, "value") else req.status
        assert status_val == RefundStatus.PENDING.value

    async def test_cannot_create_duplicate_pending_refund(self, db_session):
        user = await get_or_create_user(db_session, tg_id=30003)
        order = await _create_paid_order(db_session, user.id)
        await create_refund_request(
            db_session, user_id=user.id, order_id=order.id, reason="Причина 1",
        )
        with pytest.raises(ValueError, match="уже есть активная"):
            await create_refund_request(
                db_session, user_id=user.id, order_id=order.id, reason="Причина 2",
            )

    async def test_cannot_refund_others_order(self, db_session):
        user_a = await get_or_create_user(db_session, tg_id=30010)
        user_b = await get_or_create_user(db_session, tg_id=30011)
        order = await _create_paid_order(db_session, user_a.id)
        with pytest.raises(ValueError, match="не найден или принадлежит"):
            await create_refund_request(
                db_session, user_id=user_b.id, order_id=order.id,
                reason="Заявка с чужого аккаунта",
            )

    async def test_paid_orders_listed(self, db_session):
        user = await get_or_create_user(db_session, tg_id=30020)
        # Один paid, один unpaid
        await _create_paid_order(db_session, user.id)
        await create_order(
            db_session, user_id=user.id, work_type="referat",
            topic="Unpaid", subject="S", pages=5, deadline="01.07.2026",
        )
        paid = await get_user_paid_orders(db_session, user.id)
        assert len(paid) == 1


class TestRefundApprovalRejection:
    async def test_approve_refund_updates_order_to_refunded(self, db_session):
        user = await get_or_create_user(db_session, tg_id=40001)
        order = await _create_paid_order(db_session, user.id)
        req = await create_refund_request(
            db_session, user_id=user.id, order_id=order.id,
            reason="Полный возврат",
        )

        updated = await update_refund_status(
            db_session, req.id, RefundStatus.APPROVED,
            yukassa_refund_id="yk_refund_123",
        )
        status_val = updated.status.value if hasattr(updated.status, "value") else updated.status
        assert status_val == RefundStatus.APPROVED.value
        assert updated.yukassa_refund_id == "yk_refund_123"
        assert updated.processed_at is not None

        # Проверяем что заказ перешёл в REFUNDED
        from db.crud import get_order
        order_after = await get_order(db_session, order.id)
        order_status = order_after.status.value if hasattr(order_after.status, "value") else order_after.status
        assert order_status == OrderStatus.REFUNDED.value

    async def test_reject_refund_keeps_order_done(self, db_session):
        user = await get_or_create_user(db_session, tg_id=40002)
        order = await _create_paid_order(db_session, user.id)
        req = await create_refund_request(
            db_session, user_id=user.id, order_id=order.id,
            reason="Не подошло",
        )
        updated = await update_refund_status(
            db_session, req.id, RefundStatus.REJECTED,
            rejection_reason="Работа уже одобрена и принята",
        )
        status_val = updated.status.value if hasattr(updated.status, "value") else updated.status
        assert status_val == RefundStatus.REJECTED.value
        assert updated.rejection_reason == "Работа уже одобрена и принята"

        # Заказ должен остаться DONE
        from db.crud import get_order
        order_after = await get_order(db_session, order.id)
        order_status = order_after.status.value if hasattr(order_after.status, "value") else order_after.status
        assert order_status == OrderStatus.DONE.value

    async def test_already_processed_refund_not_changed(self, db_session):
        """Повторный update_refund_status на уже обработанной заявке — no-op."""
        user = await get_or_create_user(db_session, tg_id=40003)
        order = await _create_paid_order(db_session, user.id)
        req = await create_refund_request(
            db_session, user_id=user.id, order_id=order.id, reason="Причина",
        )
        await update_refund_status(db_session, req.id, RefundStatus.APPROVED)
        # Повторяем — должно тихо вернуть текущее состояние
        result = await update_refund_status(db_session, req.id, RefundStatus.REJECTED)
        status_val = result.status.value if hasattr(result.status, "value") else result.status
        # Статус остался APPROVED (не сменился на REJECTED)
        assert status_val == RefundStatus.APPROVED.value

    async def test_pending_refunds_count(self, db_session):
        user = await get_or_create_user(db_session, tg_id=40010)
        order1 = await _create_paid_order(db_session, user.id, amount=500.0)
        order2 = await _create_paid_order(db_session, user.id, amount=700.0)
        await create_refund_request(db_session, user.id, order1.id, reason="Первый")
        await create_refund_request(db_session, user.id, order2.id, reason="Второй")
        count = await count_pending_refund_requests(db_session)
        assert count == 2

        pending = await get_pending_refund_requests(db_session)
        assert len(pending) == 2

    async def test_has_active_refund(self, db_session):
        user = await get_or_create_user(db_session, tg_id=40020)
        order = await _create_paid_order(db_session, user.id)
        assert await has_active_refund_request(db_session, order.id) is False
        await create_refund_request(db_session, user.id, order.id, reason="X")
        assert await has_active_refund_request(db_session, order.id) is True


# ─── Правки (revisions) ─────────────────────────────────────────────────────

class TestRevisions:
    async def test_revision_only_for_done(self, db_session):
        """Правку нельзя запросить для не-DONE заказа."""
        user = await get_or_create_user(db_session, tg_id=50001)
        order = await create_order(
            db_session, user_id=user.id, work_type="referat",
            topic="T", subject="S", pages=5, deadline="01.07.2026",
        )
        # Статус по умолчанию PENDING
        from db.crud import request_revision
        success, msg = await request_revision(
            db_session, order_id=order.id, user_id=user.id,
            description="Описание правки минимум 10 символов",
        )
        assert success is False
        assert "готовых" in msg.lower()

    async def test_revision_for_done_order_works(self, db_session):
        user = await get_or_create_user(db_session, tg_id=50002)
        order = await create_order(
            db_session, user_id=user.id, work_type="referat",
            topic="T", subject="S", pages=5, deadline="01.07.2026",
        )
        await update_order_status(db_session, order.id, OrderStatus.DONE)
        from db.crud import request_revision
        success, msg = await request_revision(
            db_session, order_id=order.id, user_id=user.id,
            description="Поправить ссылки в списке литературы",
        )
        assert success is True
        from db.crud import get_order
        order_after = await get_order(db_session, order.id)
        status_val = order_after.status.value if hasattr(order_after.status, "value") else order_after.status
        assert status_val == OrderStatus.EDITING.value
        assert order_after.revision_comment is not None

    async def test_revision_max_limit(self, db_session):
        from db.crud import request_revision, MAX_REVISIONS, increment_revisions_used
        user = await get_or_create_user(db_session, tg_id=50003)
        order = await create_order(
            db_session, user_id=user.id, work_type="referat",
            topic="T", subject="S", pages=5, deadline="01.07.2026",
        )
        # Эмулируем что 2 правки уже использованы
        await update_order_status(db_session, order.id, OrderStatus.DONE)
        await increment_revisions_used(db_session, order.id)
        await increment_revisions_used(db_session, order.id)

        success, msg = await request_revision(
            db_session, order_id=order.id, user_id=user.id,
            description="Попытка третьей правки — должна быть отказана",
        )
        assert success is False
        assert "исчерпаны" in msg.lower()

    async def test_revision_not_own_order(self, db_session):
        from db.crud import request_revision
        owner = await get_or_create_user(db_session, tg_id=50010)
        intruder = await get_or_create_user(db_session, tg_id=50011)
        order = await create_order(
            db_session, user_id=owner.id, work_type="referat",
            topic="T", subject="S", pages=5, deadline="01.07.2026",
        )
        await update_order_status(db_session, order.id, OrderStatus.DONE)
        success, msg = await request_revision(
            db_session, order_id=order.id, user_id=intruder.id,
            description="Чужой пытается запросить правку",
        )
        assert success is False
        assert "не ваш" in msg.lower()

    async def test_revision_too_short(self, db_session):
        from db.crud import request_revision
        user = await get_or_create_user(db_session, tg_id=50020)
        order = await create_order(
            db_session, user_id=user.id, work_type="referat",
            topic="T", subject="S", pages=5, deadline="01.07.2026",
        )
        await update_order_status(db_session, order.id, OrderStatus.DONE)
        success, msg = await request_revision(
            db_session, order_id=order.id, user_id=user.id,
            description="Коротко",
        )
        assert success is False
        assert "10 символов" in msg

    async def test_increment_counter(self, db_session):
        from db.crud import increment_revisions_used
        user = await get_or_create_user(db_session, tg_id=50030)
        order = await create_order(
            db_session, user_id=user.id, work_type="referat",
            topic="T", subject="S", pages=5, deadline="01.07.2026",
        )
        n1 = await increment_revisions_used(db_session, order.id)
        n2 = await increment_revisions_used(db_session, order.id)
        assert n1 == 1
        assert n2 == 2
