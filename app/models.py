from __future__ import annotations

import uuid
from datetime import UTC, date, datetime
from decimal import Decimal

from sqlalchemy import (
    JSON,
    BigInteger,
    Boolean,
    CheckConstraint,
    Date,
    DateTime,
    ForeignKey,
    Index,
    Numeric,
    String,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


def utcnow() -> datetime:
    return datetime.now(UTC)


def new_id() -> str:
    return str(uuid.uuid4())


class Base(DeclarativeBase):
    pass


class User(Base):
    __tablename__ = "users"
    __table_args__ = (CheckConstraint("role IN ('OWNER', 'MANAGER', 'ACCOUNTANT', 'EMPLOYEE')"),)

    telegram_id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    role: Mapped[str] = mapped_column(String(20), default="OWNER")
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class Employee(Base):
    __tablename__ = "employees"
    __table_args__ = (
        CheckConstraint("status IN ('active', 'inactive')"),
        CheckConstraint("length(currency) = 3"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    name: Mapped[str] = mapped_column(String(200), index=True)
    telegram_id: Mapped[int | None] = mapped_column(BigInteger, unique=True)
    phone: Mapped[str | None] = mapped_column(String(50))
    payment_details: Mapped[str | None] = mapped_column(Text)
    invite_token_hash: Mapped[str | None] = mapped_column(String(64), unique=True)
    invite_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    currency: Mapped[str] = mapped_column(String(3))
    start_date: Mapped[date] = mapped_column(Date)
    status: Mapped[str] = mapped_column(String(20), default="active", index=True)
    comment: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow
    )

    rates: Mapped[list[EmployeeRate]] = relationship(
        back_populates="employee", cascade="all, delete-orphan"
    )


class EmployeeRate(Base):
    __tablename__ = "employee_rates"
    __table_args__ = (
        UniqueConstraint("employee_id", "valid_from"),
        CheckConstraint("daily_rate > 0"),
        CheckConstraint("length(currency) = 3"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    employee_id: Mapped[str] = mapped_column(ForeignKey("employees.id"), index=True)
    valid_from: Mapped[date] = mapped_column(Date)
    daily_rate: Mapped[Decimal] = mapped_column(Numeric(14, 2))
    currency: Mapped[str] = mapped_column(String(3))
    created_by: Mapped[int] = mapped_column(BigInteger)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    employee: Mapped[Employee] = relationship(back_populates="rates")


class WorkObject(Base):
    __tablename__ = "objects"
    __table_args__ = (CheckConstraint("status IN ('active', 'completed', 'archived')"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    name: Mapped[str] = mapped_column(String(200), index=True)
    address: Mapped[str | None] = mapped_column(Text)
    description: Mapped[str | None] = mapped_column(Text)
    start_date: Mapped[date] = mapped_column(Date)
    end_date: Mapped[date | None] = mapped_column(Date)
    status: Mapped[str] = mapped_column(String(20), default="active", index=True)
    comment: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow
    )


class ObjectEmployee(Base):
    __tablename__ = "object_employees"
    __table_args__ = (
        UniqueConstraint("object_id", "employee_id"),
        CheckConstraint("shift_rate > 0"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    object_id: Mapped[str] = mapped_column(ForeignKey("objects.id"), index=True)
    employee_id: Mapped[str] = mapped_column(ForeignKey("employees.id"), index=True)
    shift_rate: Mapped[Decimal | None] = mapped_column(Numeric(14, 2))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class Attendance(Base):
    __tablename__ = "attendance"
    __table_args__ = (
        UniqueConstraint("idempotency_key"),
        Index(
            "uq_active_attendance_employee_object_date",
            "employee_id",
            "object_id",
            "work_date",
            unique=True,
            sqlite_where=text("voided_at IS NULL"),
            postgresql_where=text("voided_at IS NULL"),
        ),
        CheckConstraint("coefficient > 0 AND coefficient <= 2"),
        CheckConstraint("rate_snapshot > 0"),
        CheckConstraint("earned_amount >= 0"),
        CheckConstraint("length(currency) = 3"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    employee_id: Mapped[str] = mapped_column(ForeignKey("employees.id"), index=True)
    object_id: Mapped[str] = mapped_column(ForeignKey("objects.id"), index=True)
    work_date: Mapped[date] = mapped_column(Date, index=True)
    status: Mapped[str] = mapped_column(String(20), default="worked")
    coefficient: Mapped[Decimal] = mapped_column(Numeric(5, 2))
    rate_snapshot: Mapped[Decimal | None] = mapped_column(Numeric(14, 2))
    currency: Mapped[str] = mapped_column(String(3))
    earned_amount: Mapped[Decimal | None] = mapped_column(Numeric(14, 2))
    comment: Mapped[str | None] = mapped_column(Text)
    idempotency_key: Mapped[str] = mapped_column(String(150))
    created_by: Mapped[int] = mapped_column(BigInteger)
    modified_by: Mapped[int | None] = mapped_column(BigInteger)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow
    )
    voided_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    voided_by: Mapped[int | None] = mapped_column(BigInteger)
    void_reason: Mapped[str | None] = mapped_column(Text)

    employee: Mapped[Employee] = relationship()
    object: Mapped[WorkObject] = relationship()


class Payment(Base):
    __tablename__ = "payments"
    __table_args__ = (
        UniqueConstraint("idempotency_key"),
        CheckConstraint("amount > 0"),
        CheckConstraint("length(currency) = 3"),
        CheckConstraint("method IN ('cash', 'bank', 'other')"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    employee_id: Mapped[str] = mapped_column(ForeignKey("employees.id"), index=True)
    object_id: Mapped[str | None] = mapped_column(ForeignKey("objects.id"), index=True)
    payment_date: Mapped[date] = mapped_column(Date, index=True)
    amount: Mapped[Decimal] = mapped_column(Numeric(14, 2))
    currency: Mapped[str] = mapped_column(String(3))
    method: Mapped[str] = mapped_column(String(20))
    comment: Mapped[str | None] = mapped_column(Text)
    idempotency_key: Mapped[str] = mapped_column(String(150))
    created_by: Mapped[int] = mapped_column(BigInteger)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    modified_by: Mapped[int | None] = mapped_column(BigInteger)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow
    )
    voided_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    voided_by: Mapped[int | None] = mapped_column(BigInteger)
    void_reason: Mapped[str | None] = mapped_column(Text)

    employee: Mapped[Employee] = relationship()
    object: Mapped[WorkObject | None] = relationship()


class AuditLog(Base):
    __tablename__ = "audit_log"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    actor_telegram_id: Mapped[int] = mapped_column(BigInteger, index=True)
    action: Mapped[str] = mapped_column(String(50), index=True)
    entity_type: Mapped[str] = mapped_column(String(50), index=True)
    entity_id: Mapped[str] = mapped_column(String(36), index=True)
    before: Mapped[dict | None] = mapped_column(JSON)
    after: Mapped[dict | None] = mapped_column(JSON)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class TelegramUpdate(Base):
    __tablename__ = "telegram_updates"
    __table_args__ = (CheckConstraint("status IN ('processing', 'done')"),)

    update_id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    status: Mapped[str] = mapped_column(String(20), default="processing")
    claimed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    processed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
