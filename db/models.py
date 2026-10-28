"""
ORM-модели SQLAlchemy 2.0 для всех таблиц базы данных.
Используем Mapped + mapped_column (новый стиль SQLAlchemy 2.x).
"""
import enum
from datetime import datetime
from typing import Optional
from sqlalchemy import Integer, String, Text, Float, Boolean, DateTime, ForeignKey, Enum as SAEnum, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship
from db.session import Base


# ─── Статусы заказа ──────────────────────────────────────────────────────────

class OrderStatus(str, enum.Enum):
    FILLING          = "FILLING"           # Студент заполняет бриф
    PENDING          = "PENDING"           # Ждёт решения админа
    IN_WORK          = "IN_WORK"           # Админ генерирует в Claude
    EDITING          = "EDITING"           # У редактора
    PREVIEW_SENT     = "PREVIEW_SENT"      # Студент получил PDF-превью
    AWAITING_PAYMENT = "AWAITING_PAYMENT"  # Ожидает оплаты
    DONE             = "DONE"              # Выдан чистый файл
    CANCELLED        = "CANCELLED"         # Отклонён
    REFUNDED         = "REFUNDED"          # Деньги возвращены студенту


# ─── Роли пользователей ──────────────────────────────────────────────────────

class UserRole(str, enum.Enum):
    STUDENT = "student"
    ADMIN   = "admin"
    EDITOR  = "editor"


# ─── Типы файлов ─────────────────────────────────────────────────────────────

class FileType(str, enum.Enum):
    STUDENT_TEMPLATE = "student_template"  # Шаблон/методичка от студента
    DRAFT            = "draft"             # Черновик от админа (до редактора)
    FINAL            = "final"             # Финал от редактора
    PREVIEW          = "preview"           # PDF с водяным знаком для студента


# ─── Статусы платежа ─────────────────────────────────────────────────────────

class PaymentStatus(str, enum.Enum):
    PENDING  = "pending"
    PAID     = "paid"
    REFUNDED = "refunded"


# ─── Типы реферального бонуса ─────────────────────────────────────────────────

class BonusType(str, enum.Enum):
    FIRST_ORDER = "first_order"  # +200 руб. за первую оплату приглашённого
    REPEAT      = "repeat"       # +5% от суммы заказа (со 2-й покупки)


# ─── Статусы вывода средств ───────────────────────────────────────────────────

class WithdrawalStatus(str, enum.Enum):
    PENDING  = "pending"
    APPROVED = "approved"
    REJECTED = "rejected"


# ─── Статусы заявок на возврат средств ────────────────────────────────────────

class RefundStatus(str, enum.Enum):
    PENDING  = "pending"
    APPROVED = "approved"   # одобрено + возврат проведён через ЮКассу
    REJECTED = "rejected"


# ─── Модели таблиц ───────────────────────────────────────────────────────────

class User(Base):
    """Пользователи бота (студенты, администратор, редактор)"""
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    tg_id: Mapped[int] = mapped_column(Integer, unique=True, nullable=False)
    username: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    full_name: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    role: Mapped[str] = mapped_column(
        SAEnum(UserRole, values_callable=lambda x: [e.value for e in x]),
        default=UserRole.STUDENT.value,
        nullable=False,
    )
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    is_banned: Mapped[bool] = mapped_column(Boolean, default=False)

    # Согласие с офертой (явное, при первом запуске бота)
    consented_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)

    # Для чеков ЮКассы 54-ФЗ (самозанятый): нужен email или телефон покупателя
    email: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    phone: Mapped[Optional[str]] = mapped_column(String(50), nullable=True)

    # Реферальная программа
    referred_by_id: Mapped[Optional[int]] = mapped_column(
        Integer, ForeignKey("users.id"), nullable=True
    )  # ID пользователя, который пригласил этого юзера
    ref_balance: Mapped[float] = mapped_column(Float, default=0.0)  # Накопленный баланс бонусов

    # Связи
    orders: Mapped[list["Order"]] = relationship("Order", back_populates="user")
    referral_bonuses: Mapped[list["ReferralBonus"]] = relationship(
        "ReferralBonus",
        foreign_keys="ReferralBonus.referrer_id",
        back_populates="referrer",
    )

    def __repr__(self) -> str:
        return f"<User tg_id={self.tg_id} role={self.role}>"


class Order(Base):
    """Заявки на учебные работы"""
    __tablename__ = "orders"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(Integer, ForeignKey("users.id"), nullable=False)

    # Бриф
    work_type: Mapped[Optional[str]] = mapped_column(String(100), nullable=True)
    topic: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    subject: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    pages: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    deadline: Mapped[Optional[str]] = mapped_column(String(100), nullable=True)
    comment: Mapped[Optional[str]] = mapped_column(Text, nullable=True)

    # Статус и цена
    status: Mapped[str] = mapped_column(
        SAEnum(OrderStatus, values_callable=lambda x: [e.value for e in x]),
        default=OrderStatus.FILLING.value,
        nullable=False,
    )
    price: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    prompt_text: Mapped[Optional[str]] = mapped_column(Text, nullable=True)

    # Счётчик бесплатных правок (макс 2 по оферте)
    revisions_used: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    # Последнее описание правки от клиента (для редактора)
    revision_comment: Mapped[Optional[str]] = mapped_column(Text, nullable=True)

    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)

    # Связи
    user: Mapped["User"] = relationship("User", back_populates="orders")
    files: Mapped[list["File"]] = relationship("File", back_populates="order")
    payments: Mapped[list["Payment"]] = relationship("Payment", back_populates="order")

    def __repr__(self) -> str:
        return f"<Order id={self.id} status={self.status} work_type={self.work_type}>"


class File(Base):
    """Файлы, связанные с заказами (шаблоны, черновики, финалы, превью)"""
    __tablename__ = "files"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    order_id: Mapped[int] = mapped_column(Integer, ForeignKey("orders.id"), nullable=False)
    file_type: Mapped[str] = mapped_column(
        SAEnum(FileType, values_callable=lambda x: [e.value for e in x]),
        nullable=False,
    )
    tg_file_id: Mapped[Optional[str]] = mapped_column(String(512), nullable=True)
    filename: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    local_path: Mapped[Optional[str]] = mapped_column(String(512), nullable=True)
    is_watermarked: Mapped[bool] = mapped_column(Boolean, default=False)
    uploaded_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)

    # Связи
    order: Mapped["Order"] = relationship("Order", back_populates="files")

    def __repr__(self) -> str:
        return f"<File id={self.id} order_id={self.order_id} type={self.file_type}>"


class Payment(Base):
    """Платежи через ЮКассу"""
    __tablename__ = "payments"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    order_id: Mapped[int] = mapped_column(Integer, ForeignKey("orders.id"), nullable=False)
    amount: Mapped[float] = mapped_column(Float, nullable=False)
    payment_id: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    status: Mapped[str] = mapped_column(
        SAEnum(PaymentStatus, values_callable=lambda x: [e.value for e in x]),
        default=PaymentStatus.PENDING.value,
        nullable=False,
    )
    confirmation_url: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    paid_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)

    # Связи
    order: Mapped["Order"] = relationship("Order", back_populates="payments")

    def __repr__(self) -> str:
        return f"<Payment id={self.id} order_id={self.order_id} status={self.status}>"


class ReferralBonus(Base):
    """История начислений реферальных бонусов"""
    __tablename__ = "referral_bonuses"
    # Один заказ — один бонус. Защита от повторного начисления при ретрае webhook ЮКассы.
    __table_args__ = (
        UniqueConstraint("order_id", name="uq_referral_bonus_order"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    referrer_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("users.id"), nullable=False
    )  # Кто получил бонус
    referred_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("users.id"), nullable=False
    )  # Чей заказ вызвал начисление
    order_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("orders.id"), nullable=False
    )
    bonus_type: Mapped[str] = mapped_column(
        SAEnum(BonusType, values_callable=lambda x: [e.value for e in x]),
        nullable=False,
    )
    amount: Mapped[float] = mapped_column(Float, nullable=False)  # Сумма бонуса в рублях
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)

    # Связи
    referrer: Mapped["User"] = relationship(
        "User", foreign_keys=[referrer_id], back_populates="referral_bonuses"
    )

    def __repr__(self) -> str:
        return f"<ReferralBonus referrer={self.referrer_id} type={self.bonus_type} amount={self.amount}>"


class WithdrawalRequest(Base):
    """Заявки на вывод реферального баланса"""
    __tablename__ = "withdrawal_requests"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("users.id"), nullable=False
    )
    amount: Mapped[float] = mapped_column(Float, nullable=False)
    card_details: Mapped[str] = mapped_column(String(255), nullable=False)
    status: Mapped[str] = mapped_column(
        SAEnum(WithdrawalStatus, values_callable=lambda x: [e.value for e in x]),
        default=WithdrawalStatus.PENDING.value,
        nullable=False,
    )
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    processed_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)

    # Связи
    user: Mapped["User"] = relationship("User", foreign_keys=[user_id])

    def __repr__(self) -> str:
        return f"<WithdrawalRequest id={self.id} user_id={self.user_id} amount={self.amount} status={self.status}>"


class AdminAuditLog(Base):
    """
    Журнал действий администратора в веб-панели.

    Логируется всё: попытки входа (успешные и неуспешные), принятие/отклонение
    заявок, изменение цен, отправка редактору, одобрение возвратов, выводов
    реф-баланса. Это критично для безопасности — позволяет провести post-mortem
    при подозрительной активности.
    """
    __tablename__ = "admin_audit_log"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    admin_tg_id: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)  # None при попытке несанкционированного входа
    action: Mapped[str] = mapped_column(String(64), nullable=False)  # login_success, login_failed, accept_order, send_editor, ...
    target_type: Mapped[Optional[str]] = mapped_column(String(32), nullable=True)  # order / user / refund / withdrawal / None для login
    target_id: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    ip_address: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    user_agent: Mapped[Optional[str]] = mapped_column(String(512), nullable=True)
    details: Mapped[Optional[str]] = mapped_column(Text, nullable=True)  # JSON-строка с дополнительными деталями
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, nullable=False)

    def __repr__(self) -> str:
        return f"<AdminAuditLog admin={self.admin_tg_id} action={self.action} target={self.target_type}:{self.target_id}>"


class RefundRequest(Base):
    """Заявки на возврат средств от студента.

    Один заказ — одна активная заявка (UNIQUE при status=PENDING обеспечивается
    в CRUD-функции create_refund_request через проверку существующих).
    """
    __tablename__ = "refund_requests"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    order_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("orders.id"), nullable=False
    )
    user_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("users.id"), nullable=False
    )
    reason: Mapped[str] = mapped_column(Text, nullable=False)  # причина от студента
    status: Mapped[str] = mapped_column(
        SAEnum(RefundStatus, values_callable=lambda x: [e.value for e in x]),
        default=RefundStatus.PENDING.value,
        nullable=False,
    )
    rejection_reason: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    yukassa_refund_id: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    processed_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)

    # Связи
    user: Mapped["User"] = relationship("User", foreign_keys=[user_id])
    order: Mapped["Order"] = relationship("Order", foreign_keys=[order_id])

    def __repr__(self) -> str:
        return f"<RefundRequest id={self.id} order_id={self.order_id} status={self.status}>"
