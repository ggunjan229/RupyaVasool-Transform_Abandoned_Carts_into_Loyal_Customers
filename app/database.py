# app/database.py
"""
Database layer for AI Revenue Recovery.
Auto-initializes all tables on import - safe to call multiple times (idempotent).
"""

import uuid
import enum
from datetime import datetime, timezone
from decimal import Decimal

from sqlalchemy import (
    create_engine,
    String,
    Numeric,
    Integer,
    Boolean,
    DateTime,
    ForeignKey,
    Enum as SAEnum,
    Text,
    event,
)
from sqlalchemy.orm import (
    DeclarativeBase,
    Mapped,
    mapped_column,
    relationship,
    sessionmaker,
    Session,
)

from app.config import settings


# ---------------------------------------------------------------------------
# Engine & Session
# ---------------------------------------------------------------------------

connect_args = {"check_same_thread": False}  # required for SQLite + FastAPI threads

engine = create_engine(
    settings.DATABASE_URL,
    connect_args=connect_args,
    echo=False,
    future=True,
)

# Enforce foreign key constraints in SQLite (off by default)
@event.listens_for(engine, "connect")
def _set_sqlite_pragma(dbapi_connection, connection_record):
    cursor = dbapi_connection.cursor()
    cursor.execute("PRAGMA foreign_keys=ON")
    cursor.close()


SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False, future=True)


class Base(DeclarativeBase):
    pass


def _uuid() -> str:
    return str(uuid.uuid4())


def _now() -> datetime:
    return datetime.now(timezone.utc)


# ---------------------------------------------------------------------------
# Enums
# ---------------------------------------------------------------------------

class PaymentStatus(str, enum.Enum):
    PENDING = "PENDING"
    PAID = "PAID"
    FAILED = "FAILED"


class Channel(str, enum.Enum):
    EMAIL = "Email"
    WHATSAPP = "WhatsApp"
    SMS = "SMS"


class ActionTaken(str, enum.Enum):
    SENT = "SENT"
    DISPATCH_FAILED = "DISPATCH_FAILED"
    SKIPPED_SUPPRESSED = "SKIPPED_SUPPRESSED"
    SKIPPED_OPT_OUT = "SKIPPED_OPT_OUT"
    SKIPPED_MAX_ATTEMPTS = "SKIPPED_MAX_ATTEMPTS"
    HALTED_PAID = "HALTED_PAID"
    ESCALATED_HUMAN = "ESCALATED_HUMAN"


# ---------------------------------------------------------------------------
# Table 1: Invoice (the "Cart")
# ---------------------------------------------------------------------------

class Invoice(Base):
    __tablename__ = "invoices"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)

    customer_name: Mapped[str] = mapped_column(String(255), nullable=False)
    customer_email: Mapped[str] = mapped_column(String(255), nullable=False, index=True)
    item_name: Mapped[str] = mapped_column(String(255), nullable=False)

    amount: Mapped[Decimal] = mapped_column(Numeric(12, 2), nullable=False)
    payment_status: Mapped[PaymentStatus] = mapped_column(
        SAEnum(PaymentStatus, native_enum=False),
        default=PaymentStatus.PENDING,
        nullable=False,
        index=True,
    )

    # --- "The Bar": measured recovery ---
    conversion_value_recovered: Mapped[Decimal] = mapped_column(
        Numeric(12, 2), default=Decimal("0.00"), nullable=False
    )

    # --- Stopping rule flags ---
    is_suppressed: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    opt_out: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    max_attempts_reached: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    attempt_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_now, onupdate=_now
    )

    audit_logs: Mapped[list["AgentAuditTrail"]] = relationship(
        back_populates="invoice", cascade="all, delete-orphan"
    )

    def is_recoverable(self) -> bool:
        """Central stopping-rule check - call this before ANY outbound action."""
        if self.payment_status == PaymentStatus.PAID:
            return False
        if self.is_suppressed or self.opt_out:
            return False
        if self.max_attempts_reached:
            return False
        return True

    def __repr__(self) -> str:
        return f"<Invoice {self.id[:8]} {self.customer_email} {self.payment_status}>"


# ---------------------------------------------------------------------------
# Table 2: AgentAuditTrail (compliance log)
# ---------------------------------------------------------------------------

class AgentAuditTrail(Base):
    __tablename__ = "agent_audit_trail"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    cart_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("invoices.id", ondelete="CASCADE"), nullable=False, index=True
    )

    step_number: Mapped[int] = mapped_column(Integer, nullable=False)  # 1, 2, or 3
    channel_used: Mapped[Channel] = mapped_column(
        SAEnum(Channel, native_enum=False), nullable=False
    )
    content_sent: Mapped[str] = mapped_column(Text, nullable=False)
    action_taken: Mapped[ActionTaken] = mapped_column(
        SAEnum(ActionTaken, native_enum=False), nullable=False
    )

    timestamp: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now, index=True)

    invoice: Mapped["Invoice"] = relationship(back_populates="audit_logs")

    def __repr__(self) -> str:
        return f"<AuditLog cart={self.cart_id[:8]} step={self.step_number} action={self.action_taken}>"


# ---------------------------------------------------------------------------
# Table 3: BatchRun (measured recovery per batch - required for "The Bar")
# ---------------------------------------------------------------------------

class BatchRun(Base):
    __tablename__ = "batch_runs"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)

    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    total_invoices_scanned: Mapped[int] = mapped_column(Integer, default=0)
    total_at_risk_amount: Mapped[Decimal] = mapped_column(Numeric(14, 2), default=Decimal("0.00"))
    total_recovered_amount: Mapped[Decimal] = mapped_column(Numeric(14, 2), default=Decimal("0.00"))
    total_stopped_count: Mapped[int] = mapped_column(Integer, default=0)
    total_escalated_count: Mapped[int] = mapped_column(Integer, default=0)

    status: Mapped[str] = mapped_column(String(20), default="RUNNING")  # RUNNING | COMPLETED | FAILED

    def __repr__(self) -> str:
        return f"<BatchRun {self.id[:8]} status={self.status}>"


# ---------------------------------------------------------------------------
# Init & session helper
# ---------------------------------------------------------------------------

def init_db() -> None:
    """Create all tables if they don't exist. Safe to call repeatedly."""
    Base.metadata.create_all(bind=engine)


def get_db():
    """FastAPI dependency - yields a DB session and always closes it."""
    db: Session = SessionLocal()
    try:
        yield db
    finally:
        db.close()


# Auto-init on import, as requested
init_db()