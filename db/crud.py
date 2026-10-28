"""
Асинхронные CRUD-операции для работы с базой данных.
Все функции принимают сессию AsyncSession как первый аргумент.
"""
import os
from datetime import datetime, timezone
from typing import Optional
from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from sqlalchemy import func
from db.models import User, Order, File, Payment, ReferralBonus, WithdrawalRequest, RefundRequest, UserRole, OrderStatus, FileType, PaymentStatus, BonusType, WithdrawalStatus, RefundStatus


# ─── Users ───────────────────────────────────────────────────────────────────

async def get_or_create_user(
    session: AsyncSession,
    tg_id: int,
    username: Optional[str] = None,
    full_name: Optional[str] = None,
    admin_tg_id: Optional[int] = None,
    editor_tg_id: Optional[int] = None,
) -> User:
    """
    Возвращает существующего пользователя или создаёт нового.
    Автоматически назначает роль admin/editor по tg_id из конфига.
    """
    result = await session.execute(select(User).where(User.tg_id == tg_id))
    user = result.scalar_one_or_none()

    if user is None:
        # Определяем роль по tg_id
        if admin_tg_id and tg_id == admin_tg_id:
            role = UserRole.ADMIN.value
        elif editor_tg_id and tg_id == editor_tg_id:
            role = UserRole.EDITOR.value
        else:
            role = UserRole.STUDENT.value

        user = User(
            tg_id=tg_id,
            username=username,
            full_name=full_name,
            role=role,
        )
        session.add(user)
        await session.commit()
        await session.refresh(user)
    else:
        # Обновляем имя/username при каждом обращении
        if username is not None:
            user.username = username
        if full_name is not None:
            user.full_name = full_name
        await session.commit()

    return user


async def get_user_by_tg_id(session: AsyncSession, tg_id: int) -> Optional[User]:
    result = await session.execute(select(User).where(User.tg_id == tg_id))
    return result.scalar_one_or_none()


async def update_user_email(session: AsyncSession, tg_id: int, email: str) -> None:
    """Сохраняет email пользователя для чеков ЮКассы 54-ФЗ"""
    await session.execute(
        update(User).where(User.tg_id == tg_id).values(email=email)
    )
    await session.commit()


async def update_user_phone(session: AsyncSession, tg_id: int, phone: str) -> None:
    """Сохраняет телефон пользователя для чеков ЮКассы 54-ФЗ"""
    await session.execute(
        update(User).where(User.tg_id == tg_id).values(phone=phone)
    )
    await session.commit()


async def ban_user(session: AsyncSession, tg_id: int) -> None:
    await session.execute(
        update(User).where(User.tg_id == tg_id).values(is_banned=True)
    )
    await session.commit()


async def unban_user(session: AsyncSession, tg_id: int) -> None:
    await session.execute(
        update(User).where(User.tg_id == tg_id).values(is_banned=False)
    )
    await session.commit()


async def credit_ref_balance_manual(
    session: AsyncSession,
    tg_id: int,
    amount: float,
) -> bool:
    """
    Атомарно увеличивает реф-баланс юзера на amount.
    Возвращает True, если строка обновлена (юзер существует).
    Сама причина начисления и метаданные пишутся в admin_audit_log
    через web/audit.py — поэтому отдельная таблица не нужна.
    """
    result = await session.execute(
        update(User)
        .where(User.tg_id == tg_id)
        .values(ref_balance=User.ref_balance + amount)
    )
    await session.commit()
    return result.rowcount > 0


# ─── Orders ──────────────────────────────────────────────────────────────────

async def create_order(
    session: AsyncSession,
    user_id: int,
    work_type: str,
    topic: str,
    subject: str,
    pages: int,
    deadline: str,
    comment: Optional[str] = None,
    price: Optional[float] = None,
    prompt_text: Optional[str] = None,
) -> Order:
    """Создаёт новый заказ в статусе PENDING"""
    order = Order(
        user_id=user_id,
        work_type=work_type,
        topic=topic,
        subject=subject,
        pages=pages,
        deadline=deadline,
        comment=comment,
        status=OrderStatus.PENDING.value,
        price=price,
        prompt_text=prompt_text,
    )
    session.add(order)
    await session.commit()
    await session.refresh(order)
    return order


async def get_order(session: AsyncSession, order_id: int) -> Optional[Order]:
    """Возвращает заказ со всеми связанными файлами и пользователем"""
    result = await session.execute(
        select(Order)
        .options(selectinload(Order.user), selectinload(Order.files))
        .where(Order.id == order_id)
    )
    return result.scalar_one_or_none()


async def update_order_status(
    session: AsyncSession,
    order_id: int,
    status: OrderStatus,
) -> None:
    """Обновляет статус заказа"""
    await session.execute(
        update(Order).where(Order.id == order_id).values(status=status.value)
    )
    await session.commit()


async def update_order_price(
    session: AsyncSession,
    order_id: int,
    price: float,
) -> None:
    """Обновляет цену заказа"""
    await session.execute(
        update(Order).where(Order.id == order_id).values(price=price)
    )
    await session.commit()


async def update_order_prompt(
    session: AsyncSession,
    order_id: int,
    prompt_text: str,
) -> None:
    """Сохраняет сгенерированный промпт"""
    await session.execute(
        update(Order).where(Order.id == order_id).values(prompt_text=prompt_text)
    )
    await session.commit()


async def get_user_orders(
    session: AsyncSession,
    user_id: int,
    limit: int = 10,
) -> list[Order]:
    """Возвращает последние N заказов пользователя"""
    result = await session.execute(
        select(Order)
        .where(Order.user_id == user_id)
        .order_by(Order.created_at.desc())
        .limit(limit)
    )
    return list(result.scalars().all())


async def get_all_orders(
    session: AsyncSession,
    limit: int = 20,
) -> list[Order]:
    """Возвращает все заказы (для admin)"""
    result = await session.execute(
        select(Order)
        .options(selectinload(Order.user))
        .order_by(Order.created_at.desc())
        .limit(limit)
    )
    return list(result.scalars().all())


async def get_orders_by_status(
    session: AsyncSession,
    status: OrderStatus,
) -> list[Order]:
    """Возвращает заказы с определённым статусом"""
    result = await session.execute(
        select(Order)
        .options(selectinload(Order.user))
        .where(Order.status == status.value)
        .order_by(Order.created_at.desc())
    )
    return list(result.scalars().all())


# ─── Files ───────────────────────────────────────────────────────────────────

async def save_file(
    session: AsyncSession,
    order_id: int,
    file_type: FileType,
    tg_file_id: Optional[str] = None,
    filename: Optional[str] = None,
    local_path: Optional[str] = None,
    is_watermarked: bool = False,
) -> File:
    """Сохраняет запись о файле в базу"""
    file_record = File(
        order_id=order_id,
        file_type=file_type.value,
        tg_file_id=tg_file_id,
        filename=filename,
        local_path=local_path,
        is_watermarked=is_watermarked,
    )
    session.add(file_record)
    await session.commit()
    await session.refresh(file_record)
    return file_record


async def get_file(
    session: AsyncSession,
    order_id: int,
    file_type: FileType,
) -> Optional[File]:
    """Возвращает последний файл определённого типа для заказа"""
    result = await session.execute(
        select(File)
        .where(File.order_id == order_id, File.file_type == file_type.value)
        .order_by(File.uploaded_at.desc())
        .limit(1)
    )
    return result.scalar_one_or_none()


async def get_all_order_files(
    session: AsyncSession,
    order_id: int,
) -> list[File]:
    """Возвращает все файлы заказа"""
    result = await session.execute(
        select(File).where(File.order_id == order_id).order_by(File.uploaded_at)
    )
    return list(result.scalars().all())


async def get_files_by_type(
    session: AsyncSession,
    order_id: int,
    file_type: FileType,
) -> list[File]:
    """Возвращает все файлы заказа определённого типа (строго по порядку загрузки).

    Тай-брейк по id: если у нескольких файлов совпадает uploaded_at (загрузка
    одной пачкой), порядок остаётся детерминированным — иначе при склейке
    превью страницы могли бы «переставиться местами».
    """
    result = await session.execute(
        select(File)
        .where(File.order_id == order_id, File.file_type == file_type.value)
        .order_by(File.uploaded_at, File.id)
    )
    return list(result.scalars().all())


async def delete_files_by_type(
    session: AsyncSession,
    order_id: int,
    file_type: FileType,
) -> None:
    """Удаляет записи о файлах данного типа вместе с локальными файлами на диске.

    Используется перед пересозданием превью, чтобы не накапливались устаревшие
    PREVIEW-файлы и студенту не уходили старые версии.
    """
    files = await get_files_by_type(session, order_id, file_type)
    for f in files:
        if f.local_path:
            try:
                os.remove(f.local_path)
            except OSError:
                pass
        await session.delete(f)
    await session.commit()


# ─── Payments ────────────────────────────────────────────────────────────────

async def create_payment(
    session: AsyncSession,
    order_id: int,
    amount: float,
    payment_id: Optional[str] = None,
    confirmation_url: Optional[str] = None,
) -> Payment:
    """Создаёт запись о платеже"""
    payment = Payment(
        order_id=order_id,
        amount=amount,
        payment_id=payment_id,
        status=PaymentStatus.PENDING.value,
        confirmation_url=confirmation_url,
    )
    session.add(payment)
    await session.commit()
    await session.refresh(payment)
    return payment


async def update_payment_status(
    session: AsyncSession,
    yukassa_payment_id: str,
    status: PaymentStatus,
) -> Optional[Payment]:
    """
    Обновляет статус платежа по ID из ЮКассы.
    Возвращает объект Payment или None если не найден.
    """
    result = await session.execute(
        select(Payment).where(Payment.payment_id == yukassa_payment_id)
    )
    payment = result.scalar_one_or_none()
    if payment is None:
        return None

    payment.status = status.value
    if status == PaymentStatus.PAID:
        payment.paid_at = datetime.now(timezone.utc).replace(tzinfo=None)

    await session.commit()
    await session.refresh(payment)
    return payment


async def get_payment_by_yukassa_id(
    session: AsyncSession,
    yukassa_payment_id: str,
) -> Optional[Payment]:
    """Возвращает платёж по ID из ЮКассы"""
    result = await session.execute(
        select(Payment).where(Payment.payment_id == yukassa_payment_id)
    )
    return result.scalar_one_or_none()


# ─── Referrals ────────────────────────────────────────────────────────────────

async def set_referrer(
    session: AsyncSession,
    user_id: int,
    referrer_tg_id: int,
) -> bool:
    """
    Записывает, кто пригласил пользователя. Вызывается один раз при регистрации.
    Защита от само-реферала и повторной записи.

    :return: True если реферер успешно сохранён, False если уже был или само-реферал
    """
    # Получаем пользователя
    result = await session.execute(select(User).where(User.id == user_id))
    user = result.scalar_one_or_none()
    if user is None or user.referred_by_id is not None:
        return False  # Уже есть реферер

    # Ищем реферера по tg_id
    result = await session.execute(select(User).where(User.tg_id == referrer_tg_id))
    referrer = result.scalar_one_or_none()
    if referrer is None or referrer.id == user_id:
        return False  # Реферер не найден или само-реферал

    user.referred_by_id = referrer.id
    await session.commit()
    return True


async def count_paid_orders(session: AsyncSession, user_id: int) -> int:
    """
    Считает количество оплаченных (DONE) заказов пользователя.
    Используется для определения типа реферального бонуса.
    """
    result = await session.execute(
        select(func.count(Order.id))
        .where(Order.user_id == user_id, Order.status == OrderStatus.DONE.value)
    )
    return result.scalar_one() or 0


async def add_referral_bonus(
    session: AsyncSession,
    referrer_id: int,
    referred_id: int,
    order_id: int,
    bonus_type: BonusType,
    amount: float,
) -> Optional[ReferralBonus]:
    """
    Начисляет реферальный бонус (идемпотентно):
    - записывает строку в referral_bonuses (UNIQUE(order_id))
    - прибавляет amount к ref_balance реферера

    Если бонус по этому order_id уже начислен — возвращает существующую запись
    и НЕ изменяет баланс. Это защита от повторного webhook-ретрая ЮКассы.
    """
    # Проверяем, не начислен ли уже бонус по этому заказу
    existing = await session.execute(
        select(ReferralBonus).where(ReferralBonus.order_id == order_id)
    )
    existing_bonus = existing.scalar_one_or_none()
    if existing_bonus is not None:
        return existing_bonus

    bonus = ReferralBonus(
        referrer_id=referrer_id,
        referred_id=referred_id,
        order_id=order_id,
        bonus_type=bonus_type.value,
        amount=amount,
    )
    session.add(bonus)

    try:
        # Flush триггерит UNIQUE-проверку до commit
        await session.flush()
    except IntegrityError:
        # Параллельная гонка: бонус был начислен другим webhook
        await session.rollback()
        existing = await session.execute(
            select(ReferralBonus).where(ReferralBonus.order_id == order_id)
        )
        return existing.scalar_one_or_none()

    # Обновляем баланс реферера
    await session.execute(
        update(User)
        .where(User.id == referrer_id)
        .values(ref_balance=User.ref_balance + amount)
    )
    await session.commit()
    await session.refresh(bonus)
    return bonus


async def get_referral_stats(
    session: AsyncSession,
    referrer_id: int,
) -> dict:
    """
    Возвращает статистику реферальной программы для пользователя:
    - balance: текущий баланс
    - total_earned: всего начислено
    - referrals_count: количество приглашённых пользователей
    - bonuses: список последних начислений
    """
    # Баланс пользователя
    result = await session.execute(
        select(User.ref_balance).where(User.id == referrer_id)
    )
    balance = result.scalar_one_or_none() or 0.0

    # Всего начислено
    result = await session.execute(
        select(func.sum(ReferralBonus.amount))
        .where(ReferralBonus.referrer_id == referrer_id)
    )
    total_earned = result.scalar_one() or 0.0

    # Количество приглашённых
    result = await session.execute(
        select(func.count(User.id)).where(User.referred_by_id == referrer_id)
    )
    referrals_count = result.scalar_one() or 0

    # Последние 5 начислений
    result = await session.execute(
        select(ReferralBonus)
        .where(ReferralBonus.referrer_id == referrer_id)
        .order_by(ReferralBonus.created_at.desc())
        .limit(5)
    )
    bonuses = list(result.scalars().all())

    return {
        "balance": balance,
        "total_earned": total_earned,
        "referrals_count": referrals_count,
        "bonuses": bonuses,
    }

# ─── Withdrawals ──────────────────────────────────────────────────────────────

async def create_withdrawal_request(
    session: AsyncSession,
    user_id: int,
    amount: float,
    card_details: str,
) -> WithdrawalRequest:
    """
    Создаёт заявку на вывод и АТОМАРНО списывает сумму с баланса.

    Защита от race condition: SQL-условие `ref_balance >= amount` в WHERE
    гарантирует, что при параллельных запросах баланс не уйдёт в минус.
    При недостатке средств бросает ValueError.
    """
    # Атомарно списываем баланс — UPDATE ... WHERE ref_balance >= amount
    result = await session.execute(
        update(User)
        .where(User.id == user_id, User.ref_balance >= amount)
        .values(ref_balance=User.ref_balance - amount)
    )

    if result.rowcount == 0:
        # Не хватило средств. Нет смысла rollback — мы ничего не закоммитили.
        # Но нужно убрать pending-изменения из сессии
        raise ValueError("Недостаточно средств на балансе или конкурентный запрос")

    request = WithdrawalRequest(
        user_id=user_id,
        amount=amount,
        card_details=card_details,
        status=WithdrawalStatus.PENDING.value,
    )
    session.add(request)

    await session.commit()
    await session.refresh(request)
    return request


async def get_last_withdrawal_request(
    session: AsyncSession,
    user_id: int,
) -> Optional[WithdrawalRequest]:
    """Возвращает последнюю заявку пользователя (любого статуса кроме отклонённой)"""
    result = await session.execute(
        select(WithdrawalRequest)
        .where(
            WithdrawalRequest.user_id == user_id,
            WithdrawalRequest.status != WithdrawalStatus.REJECTED.value
        )
        .order_by(WithdrawalRequest.created_at.desc())
        .limit(1)
    )
    return result.scalar_one_or_none()


async def get_pending_withdrawals(
    session: AsyncSession,
    limit: int = 10,
    offset: int = 0,
) -> list[WithdrawalRequest]:
    """Возвращает заявки на вывод в статусе PENDING с пагинацией"""
    result = await session.execute(
        select(WithdrawalRequest)
        .options(selectinload(WithdrawalRequest.user))
        .where(WithdrawalRequest.status == WithdrawalStatus.PENDING.value)
        .order_by(WithdrawalRequest.created_at.asc())
        .limit(limit)
        .offset(offset)
    )
    return list(result.scalars().all())


async def count_pending_withdrawals(session: AsyncSession) -> int:
    """Считает общее количество PENDING заявок на вывод"""
    result = await session.execute(
        select(func.count(WithdrawalRequest.id))
        .where(WithdrawalRequest.status == WithdrawalStatus.PENDING.value)
    )
    return result.scalar_one() or 0


async def update_withdrawal_status(
    session: AsyncSession,
    request_id: int,
    status: WithdrawalStatus,
) -> Optional[WithdrawalRequest]:
    """
    Обновляет статус заявки.
    Если статус REJECTED, то возвращает сумму обратно на баланс пользователя.
    """
    result = await session.execute(
        select(WithdrawalRequest).where(WithdrawalRequest.id == request_id)
    )
    req = result.scalar_one_or_none()
    
    if req is None or req.status != WithdrawalStatus.PENDING.value:
        return req

    req.status = status.value
    req.processed_at = datetime.now(timezone.utc).replace(tzinfo=None)

    if status == WithdrawalStatus.REJECTED:
        await session.execute(
            update(User)
            .where(User.id == req.user_id)
            .values(ref_balance=User.ref_balance + req.amount)
        )

    await session.commit()
    await session.refresh(req)
    return req


# ─── Analytics ────────────────────────────────────────────────────────────────

async def get_admin_analytics(session: AsyncSession) -> dict:
    """
    Агрегированная статистика для дашборда администратора.
    Все запросы используют COUNT/SUM — не загружают строки целиком.
    """
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    today_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    week_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    from datetime import timedelta
    week_start = today_start - timedelta(days=now.weekday())
    month_start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)

    # Всего студентов
    r = await session.execute(
        select(func.count(User.id)).where(User.role == UserRole.STUDENT.value)
    )
    total_users = r.scalar_one() or 0

    # Новые студенты за периоды
    r = await session.execute(
        select(func.count(User.id))
        .where(User.role == UserRole.STUDENT.value, User.created_at >= today_start)
    )
    new_today = r.scalar_one() or 0

    r = await session.execute(
        select(func.count(User.id))
        .where(User.role == UserRole.STUDENT.value, User.created_at >= week_start)
    )
    new_week = r.scalar_one() or 0

    r = await session.execute(
        select(func.count(User.id))
        .where(User.role == UserRole.STUDENT.value, User.created_at >= month_start)
    )
    new_month = r.scalar_one() or 0

    # Всего заказов
    r = await session.execute(select(func.count(Order.id)))
    total_orders = r.scalar_one() or 0

    # Заказы по статусам
    orders_by_status = {}
    for status in OrderStatus:
        r = await session.execute(
            select(func.count(Order.id)).where(Order.status == status.value)
        )
        orders_by_status[status.value] = r.scalar_one() or 0

    # Общая выручка (все PAID платежи)
    r = await session.execute(
        select(func.coalesce(func.sum(Payment.amount), 0.0))
        .where(Payment.status == PaymentStatus.PAID.value)
    )
    total_revenue = float(r.scalar_one() or 0.0)

    # Выручка за текущий месяц
    r = await session.execute(
        select(func.coalesce(func.sum(Payment.amount), 0.0))
        .where(Payment.status == PaymentStatus.PAID.value, Payment.paid_at >= month_start)
    )
    revenue_month = float(r.scalar_one() or 0.0)

    # Заявки на вывод: кол-во и сумма
    r = await session.execute(
        select(func.count(WithdrawalRequest.id))
        .where(WithdrawalRequest.status == WithdrawalStatus.PENDING.value)
    )
    pending_wd_count = r.scalar_one() or 0

    r = await session.execute(
        select(func.coalesce(func.sum(WithdrawalRequest.amount), 0.0))
        .where(WithdrawalRequest.status == WithdrawalStatus.PENDING.value)
    )
    pending_wd_sum = float(r.scalar_one() or 0.0)

    # Топ-5 рефереров по балансу
    r = await session.execute(
        select(User)
        .where(User.role == UserRole.STUDENT.value, User.ref_balance > 0)
        .order_by(User.ref_balance.desc())
        .limit(5)
    )
    top_referrers = list(r.scalars().all())

    return {
        "total_users": total_users,
        "new_today": new_today,
        "new_week": new_week,
        "new_month": new_month,
        "total_orders": total_orders,
        "orders_by_status": orders_by_status,
        "total_revenue": total_revenue,
        "revenue_month": revenue_month,
        "pending_wd_count": pending_wd_count,
        "pending_wd_sum": pending_wd_sum,
        "top_referrers": top_referrers,
    }


async def get_all_referral_debtors(session: AsyncSession) -> list[dict]:
    """
    Возвращает всех пользователей с ненулевым ref_balance
    (т.е. тех, кому администратор должен заплатить).
    Включает: пользователя, сумму баланса, кол-во приглашённых, последние бонусы.
    """
    r = await session.execute(
        select(User)
        .where(User.ref_balance > 0)
        .order_by(User.ref_balance.desc())
    )
    users = list(r.scalars().all())

    result = []
    for u in users:
        r2 = await session.execute(
            select(func.count(User.id)).where(User.referred_by_id == u.id)
        )
        invited_count = r2.scalar_one() or 0

        r3 = await session.execute(
            select(ReferralBonus)
            .where(ReferralBonus.referrer_id == u.id)
            .order_by(ReferralBonus.created_at.desc())
            .limit(3)
        )
        recent_bonuses = list(r3.scalars().all())

        result.append({
            "user": u,
            "balance": u.ref_balance,
            "invited_count": invited_count,
            "recent_bonuses": recent_bonuses,
        })

    return result


# ─── Согласие с офертой ──────────────────────────────────────────────────────

async def set_user_consent(session: AsyncSession, tg_id: int) -> None:
    """Сохраняет факт согласия пользователя с офертой (текущая дата/время)."""
    await session.execute(
        update(User)
        .where(User.tg_id == tg_id)
        .values(consented_at=datetime.now(timezone.utc).replace(tzinfo=None))
    )
    await session.commit()


# ─── Отмена заказа студентом ─────────────────────────────────────────────────

async def cancel_order_by_student(
    session: AsyncSession,
    order_id: int,
    user_id: int,
) -> tuple[bool, str]:
    """
    Отменяет заказ от лица студента. Разрешено ТОЛЬКО на статусе PENDING.

    Возвращает (success, message):
    - (True, "") — успех
    - (False, "Заказ не найден")
    - (False, "Не ваш заказ")
    - (False, "Отмена возможна только до приёма заявки администратором")
    """
    result = await session.execute(
        select(Order).where(Order.id == order_id)
    )
    order = result.scalar_one_or_none()
    if order is None:
        return False, "Заказ не найден"
    if order.user_id != user_id:
        return False, "Не ваш заказ"

    status_val = order.status.value if hasattr(order.status, "value") else order.status
    if status_val != OrderStatus.PENDING.value:
        return False, (
            "Отмена возможна только до приёма заявки администратором. "
            "Если заказ уже оплачен — используйте /refund. "
            "Если работа в процессе — напишите в поддержку: /support."
        )

    order.status = OrderStatus.CANCELLED.value
    await session.commit()
    return True, ""


# ─── Refund (заявки на возврат) ─────────────────────────────────────────────

async def get_user_paid_orders(session: AsyncSession, user_id: int) -> list[Order]:
    """
    Возвращает заказы пользователя, по которым есть оплаченные платежи
    и которые ещё не возвращены.
    """
    result = await session.execute(
        select(Order)
        .options(selectinload(Order.payments))
        .where(
            Order.user_id == user_id,
            Order.status.in_([
                OrderStatus.AWAITING_PAYMENT.value,
                OrderStatus.DONE.value,
            ]),
        )
        .order_by(Order.created_at.desc())
    )
    orders = list(result.scalars().all())
    # Оставляем только те, где хотя бы один платёж PAID
    return [
        o for o in orders
        if any(
            (p.status.value if hasattr(p.status, "value") else p.status) == PaymentStatus.PAID.value
            for p in o.payments
        )
    ]


async def has_active_refund_request(session: AsyncSession, order_id: int) -> bool:
    """Проверяет, есть ли уже активная (PENDING) или одобренная заявка по этому заказу."""
    r = await session.execute(
        select(RefundRequest).where(
            RefundRequest.order_id == order_id,
            RefundRequest.status.in_([
                RefundStatus.PENDING.value,
                RefundStatus.APPROVED.value,
            ]),
        )
    )
    return r.scalar_one_or_none() is not None


async def create_refund_request(
    session: AsyncSession,
    user_id: int,
    order_id: int,
    reason: str,
) -> RefundRequest:
    """
    Создаёт заявку на возврат. Проверяет:
    - заказ принадлежит юзеру
    - есть PAID-платёж по заказу
    - нет активной заявки

    Бросает ValueError при нарушении инвариантов.
    """
    result = await session.execute(
        select(Order)
        .options(selectinload(Order.payments))
        .where(Order.id == order_id)
    )
    order = result.scalar_one_or_none()
    if order is None or order.user_id != user_id:
        raise ValueError("Заказ не найден или принадлежит другому пользователю")

    paid_payment = None
    for p in order.payments:
        p_status = p.status.value if hasattr(p.status, "value") else p.status
        if p_status == PaymentStatus.PAID.value:
            paid_payment = p
            break
    if paid_payment is None:
        raise ValueError("По заказу нет оплаченных платежей")

    if await has_active_refund_request(session, order_id):
        raise ValueError("По этому заказу уже есть активная заявка на возврат")

    req = RefundRequest(
        order_id=order_id,
        user_id=user_id,
        reason=reason,
        status=RefundStatus.PENDING.value,
    )
    session.add(req)
    await session.commit()
    await session.refresh(req)
    return req


async def get_refund_request(
    session: AsyncSession,
    refund_id: int,
) -> Optional[RefundRequest]:
    """Возвращает заявку на возврат с подгруженным user и order (с payments)."""
    r = await session.execute(
        select(RefundRequest)
        .options(
            selectinload(RefundRequest.user),
            selectinload(RefundRequest.order).selectinload(Order.payments),
        )
        .where(RefundRequest.id == refund_id)
    )
    return r.scalar_one_or_none()


async def update_refund_status(
    session: AsyncSession,
    refund_id: int,
    status: RefundStatus,
    yukassa_refund_id: Optional[str] = None,
    rejection_reason: Optional[str] = None,
) -> Optional[RefundRequest]:
    """
    Обновляет статус заявки на возврат.
    При APPROVED — также переводит заказ в REFUNDED.
    """
    result = await session.execute(
        select(RefundRequest)
        .options(selectinload(RefundRequest.order))
        .where(RefundRequest.id == refund_id)
    )
    req = result.scalar_one_or_none()
    if req is None:
        return None

    current_status = req.status.value if hasattr(req.status, "value") else req.status
    if current_status != RefundStatus.PENDING.value:
        return req  # уже обработан

    req.status = status.value
    req.processed_at = datetime.now(timezone.utc).replace(tzinfo=None)
    if yukassa_refund_id:
        req.yukassa_refund_id = yukassa_refund_id
    if rejection_reason:
        req.rejection_reason = rejection_reason

    if status == RefundStatus.APPROVED:
        # Переводим заказ в REFUNDED
        await session.execute(
            update(Order)
            .where(Order.id == req.order_id)
            .values(status=OrderStatus.REFUNDED.value)
        )

    await session.commit()
    await session.refresh(req)
    return req


async def get_pending_refund_requests(
    session: AsyncSession,
    limit: int = 10,
    offset: int = 0,
) -> list[RefundRequest]:
    """Возвращает все ожидающие обработки заявки на возврат."""
    r = await session.execute(
        select(RefundRequest)
        .options(
            selectinload(RefundRequest.user),
            selectinload(RefundRequest.order),
        )
        .where(RefundRequest.status == RefundStatus.PENDING.value)
        .order_by(RefundRequest.created_at.asc())
        .limit(limit)
        .offset(offset)
    )
    return list(r.scalars().all())


async def count_pending_refund_requests(session: AsyncSession) -> int:
    """Количество ожидающих заявок на возврат."""
    r = await session.execute(
        select(func.count(RefundRequest.id))
        .where(RefundRequest.status == RefundStatus.PENDING.value)
    )
    return r.scalar_one() or 0


# ─── Правки (revisions) ──────────────────────────────────────────────────────

# Максимум бесплатных правок (из оферты)
MAX_REVISIONS = 2


async def request_revision(
    session: AsyncSession,
    order_id: int,
    user_id: int,
    description: str,
) -> tuple[bool, str]:
    """
    Студент запрашивает правку готового заказа.

    Условия:
    - Заказ должен принадлежать пользователю
    - Статус должен быть DONE (правки только готовых работ)
    - revisions_used < MAX_REVISIONS

    При успехе: сохраняет описание правки, переводит статус в EDITING.
    Возвращает (success, message).
    """
    result = await session.execute(
        select(Order).where(Order.id == order_id)
    )
    order = result.scalar_one_or_none()
    if order is None:
        return False, "Заказ не найден"
    if order.user_id != user_id:
        return False, "Это не ваш заказ"

    status_val = order.status.value if hasattr(order.status, "value") else order.status
    if status_val != OrderStatus.DONE.value:
        return False, (
            "Правки доступны только для готовых заказов (статус «Выполнен»). "
            "Если возникли вопросы по работе в процессе — напишите в /support."
        )

    if order.revisions_used >= MAX_REVISIONS:
        return False, (
            f"Бесплатные правки исчерпаны ({order.revisions_used}/{MAX_REVISIONS}). "
            "Для дополнительных правок свяжитесь с поддержкой /support — "
            "обсудим индивидуально."
        )

    if len((description or "").strip()) < 10:
        return False, "Опишите правку подробнее — минимум 10 символов"

    # Сохраняем описание правки и переводим в EDITING (редактор увидит)
    order.revision_comment = description.strip()
    order.status = OrderStatus.EDITING.value
    await session.commit()
    return True, ""


async def increment_revisions_used(session: AsyncSession, order_id: int) -> int:
    """
    Увеличивает счётчик использованных правок на 1 и возвращает новое значение.
    Вызывается когда редактор отправил исправленный файл клиенту.
    """
    await session.execute(
        update(Order)
        .where(Order.id == order_id)
        .values(revisions_used=Order.revisions_used + 1)
    )
    await session.commit()
    r = await session.execute(select(Order.revisions_used).where(Order.id == order_id))
    return r.scalar_one() or 0
