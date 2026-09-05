from __future__ import annotations

import hashlib
import secrets
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from typing import ClassVar

from sqlalchemy import func, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, sessionmaker

from .models import (
    Attendance,
    AuditLog,
    Employee,
    EmployeeRate,
    ObjectEmployee,
    Payment,
    TelegramUpdate,
    User,
    WorkObject,
)

CENT = Decimal("0.01")
MONEY_MAX = Decimal("9999999999.99")


class DomainError(ValueError):
    pass


class NotFound(DomainError):
    pass


class Conflict(DomainError):
    pass


def money(value: str | Decimal | int) -> Decimal:
    try:
        result = Decimal(str(value).replace(",", ".")).quantize(CENT, ROUND_HALF_UP)
    except InvalidOperation as exc:
        raise DomainError("Некорректная денежная сумма.") from exc
    if not result.is_finite():
        raise DomainError("Некорректная денежная сумма.")
    if result <= 0:
        raise DomainError("Сумма должна быть больше нуля.")
    if result > MONEY_MAX:
        raise DomainError("Сумма превышает допустимый предел.")
    return result


def clean_text(
    value: str | None, field: str, max_length: int, *, required: bool = False
) -> str | None:
    result = str(value or "").strip()
    if required and not result:
        raise DomainError(f"{field} обязательно.")
    if len(result) > max_length:
        raise DomainError(f"{field}: максимум {max_length} символов.")
    return result or None


def day_coefficient(value: str | Decimal) -> Decimal:
    try:
        result = Decimal(str(value).replace(",", ".")).quantize(CENT, ROUND_HALF_UP)
    except InvalidOperation as exc:
        raise DomainError("Некорректный коэффициент дня.") from exc
    if result <= 0 or result > Decimal("2.00"):
        raise DomainError("Коэффициент должен быть больше 0 и не больше 2.")
    return result


def as_money(value: Decimal | int | None) -> Decimal:
    return Decimal(value or 0).quantize(CENT, ROUND_HALF_UP)


def audit(
    session: Session,
    actor: int,
    action: str,
    entity_type: str,
    entity_id: str,
    *,
    before: dict | None = None,
    after: dict | None = None,
) -> None:
    session.add(
        AuditLog(
            actor_telegram_id=actor,
            action=action,
            entity_type=entity_type,
            entity_id=entity_id,
            before=before,
            after=after,
        )
    )


@dataclass(frozen=True)
class AttendancePreview:
    employee_id: str
    employee_name: str
    coefficient: Decimal
    rate: Decimal | None
    earned: Decimal | None
    currency: str


@dataclass(frozen=True)
class PayrollSummary:
    employee_id: str
    employee_name: str
    currency: str
    days: Decimal
    earned: Decimal
    paid: Decimal
    unrated_shifts: int = 0

    @property
    def balance(self) -> Decimal:
        return as_money(self.earned - self.paid)


class EmployeeService:
    def __init__(self, sessions: sessionmaker[Session]):
        self.sessions = sessions

    def create_invite(self, employee_id: str, *, actor: int) -> str:
        token = secrets.token_urlsafe(24)
        with self.sessions() as session, session.begin():
            employee = session.scalar(
                update(Employee)
                .where(
                    Employee.id == employee_id,
                    Employee.status == "active",
                    Employee.telegram_id.is_(None),
                )
                .values(
                    invite_token_hash=hashlib.sha256(token.encode()).hexdigest(),
                    invite_expires_at=datetime.now(UTC) + timedelta(days=7),
                )
                .returning(Employee)
            )
            if employee is None:
                raise Conflict("Приглашение доступно только активному сотруднику без Telegram ID.")
            audit(session, actor, "employee_invited", "employee", employee.id)
        return token

    def accept_invite(self, token: str, *, telegram_id: int) -> Employee:
        if telegram_id <= 0 or not token or len(token) > 64:
            raise DomainError("Некорректное приглашение.")
        try:
            with self.sessions() as session, session.begin():
                if session.scalar(select(Employee.id).where(Employee.telegram_id == telegram_id)):
                    raise Conflict(
                        "Ваш Telegram уже связан с карточкой сотрудника. Откройте /start."
                    )
                employee = session.scalar(
                    update(Employee)
                    .where(
                        Employee.invite_token_hash == hashlib.sha256(token.encode()).hexdigest(),
                        Employee.invite_expires_at > datetime.now(UTC),
                        Employee.telegram_id.is_(None),
                        Employee.status == "active",
                    )
                    .values(telegram_id=telegram_id, invite_token_hash=None, invite_expires_at=None)
                    .returning(Employee)
                )
                if employee is None:
                    raise Conflict(
                        "Ссылка недействительна или уже использована. Запросите новую у владельца."
                    )
                audit(session, telegram_id, "employee_linked", "employee", employee.id)
            return employee
        except IntegrityError as exc:
            raise Conflict("Ваш Telegram уже связан с карточкой сотрудника.") from exc

    def by_telegram(self, telegram_id: int) -> Employee | None:
        with self.sessions() as session:
            return session.scalar(
                select(Employee).where(
                    Employee.telegram_id == telegram_id, Employee.status == "active"
                )
            )

    def update_own_profile(self, telegram_id: int, *, name: str, payment_details: str) -> Employee:
        name = clean_text(name, "ФИО", 200, required=True)
        payment_details = clean_text(payment_details, "Реквизиты", 1000, required=True)
        with self.sessions() as session, session.begin():
            employee = session.scalar(
                update(Employee)
                .where(Employee.telegram_id == telegram_id, Employee.status == "active")
                .values(name=name, payment_details=payment_details)
                .returning(Employee)
            )
            if employee is None:
                raise NotFound("Нет доступа к карточке. Обратитесь к владельцу.")
            audit(session, telegram_id, "employee_profile_updated", "employee", employee.id)
        return employee

    def create(
        self,
        *,
        name: str,
        rate: str | Decimal | None = None,
        currency: str,
        start_date: date,
        actor: int,
        phone: str | None = None,
        telegram_id: int | None = None,
        comment: str | None = None,
        payment_details: str | None = None,
        object_id: str | None = None,
        shift_rate: str | Decimal | None = None,
    ) -> Employee:
        name = clean_text(name, "Имя", 200, required=True)
        phone = clean_text(phone, "Телефон", 50)
        comment = clean_text(comment, "Комментарий", 4000)
        payment_details = clean_text(payment_details, "Реквизиты", 1000)
        currency = currency.strip().upper()
        if len(currency) != 3 or not currency.isascii() or not currency.isalpha():
            raise DomainError("Валюта должна быть трёхбуквенным ISO-кодом.")
        if telegram_id is not None and telegram_id <= 0:
            raise DomainError("Telegram ID должен быть положительным числом.")
        daily_rate = money(rate) if rate is not None else None
        object_rate = money(shift_rate) if shift_rate is not None else None
        with self.sessions() as session, session.begin():
            if object_id is not None:
                obj = session.get(WorkObject, object_id)
                if obj is None or obj.status != "active":
                    raise NotFound("Активный объект не найден.")
            if telegram_id is not None and session.scalar(
                select(Employee.id).where(Employee.telegram_id == telegram_id)
            ):
                raise Conflict("Этот Telegram ID уже связан с другим сотрудником.")
            employee = Employee(
                name=name,
                phone=phone,
                telegram_id=telegram_id,
                currency=currency,
                start_date=start_date,
                comment=comment,
                payment_details=payment_details,
            )
            session.add(employee)
            session.flush()
            if daily_rate is not None:
                session.add(
                    EmployeeRate(
                        employee_id=employee.id,
                        valid_from=start_date,
                        daily_rate=daily_rate,
                        currency=currency,
                        created_by=actor,
                    )
                )
            if object_id is not None:
                session.add(
                    ObjectEmployee(
                        employee_id=employee.id, object_id=object_id, shift_rate=object_rate
                    )
                )
                audit(
                    session,
                    actor,
                    "employee_added_to_object",
                    "employee",
                    employee.id,
                    after={
                        "object_id": object_id,
                        "shift_rate": str(object_rate) if object_rate else None,
                    },
                )
            audit(
                session,
                actor,
                "employee_created",
                "employee",
                employee.id,
                after={"name": name, "rate": str(daily_rate), "currency": currency},
            )
        return employee

    def update(
        self,
        employee_id: str,
        *,
        name: str,
        phone: str | None,
        telegram_id: int | None,
        comment: str | None,
        actor: int,
        payment_details: str | None = None,
        start_date: date | None = None,
    ) -> Employee:
        name = clean_text(name, "Имя", 200, required=True)
        phone = clean_text(phone, "Телефон", 50)
        comment = clean_text(comment, "Комментарий", 4000)
        update_payment_details = payment_details is not None
        payment_details = clean_text(payment_details, "Реквизиты", 1000)
        if telegram_id is not None and telegram_id <= 0:
            raise DomainError("Telegram ID должен быть положительным числом.")
        try:
            with self.sessions() as session, session.begin():
                employee = session.get(Employee, employee_id)
                if employee is None:
                    raise NotFound("Сотрудник не найден.")
                if start_date is not None:
                    first_shift = session.scalar(
                        select(func.min(Attendance.work_date)).where(
                            Attendance.employee_id == employee_id
                        )
                    )
                    first_rate = session.scalar(
                        select(func.min(EmployeeRate.valid_from)).where(
                            EmployeeRate.employee_id == employee_id
                        )
                    )
                    if any(
                        start_date > earliest for earliest in (first_shift, first_rate) if earliest
                    ):
                        raise DomainError(
                            "Дата начала не может быть позже существующих смен или ставок."
                        )
                before = {
                    "name": employee.name,
                    "phone": employee.phone,
                    "telegram_id": employee.telegram_id,
                    "comment": employee.comment,
                    "payment_details": employee.payment_details,
                    "start_date": employee.start_date.isoformat(),
                }
                employee.name = name
                employee.phone = phone
                if employee.telegram_id != telegram_id:
                    employee.invite_token_hash = None
                    employee.invite_expires_at = None
                employee.telegram_id = telegram_id
                employee.comment = comment
                if start_date is not None:
                    employee.start_date = start_date
                if update_payment_details:
                    employee.payment_details = payment_details
                session.flush()
                audit(
                    session,
                    actor,
                    "employee_updated",
                    "employee",
                    employee.id,
                    before=before,
                    after={
                        "name": name,
                        "phone": phone,
                        "telegram_id": telegram_id,
                        "comment": comment,
                        "payment_details": employee.payment_details,
                        "start_date": employee.start_date.isoformat(),
                    },
                )
            return employee
        except IntegrityError as exc:
            raise Conflict("Этот Telegram ID уже связан с другим сотрудником.") from exc

    def set_status(self, employee_id: str, status: str, *, actor: int) -> Employee:
        if status not in {"active", "inactive"}:
            raise DomainError("Некорректный статус сотрудника.")
        with self.sessions() as session, session.begin():
            employee = session.get(Employee, employee_id)
            if employee is None:
                raise NotFound("Сотрудник не найден.")
            before = employee.status
            employee.status = status
            if status == "inactive":
                employee.invite_token_hash = None
                employee.invite_expires_at = None
            audit(
                session,
                actor,
                "employee_status_changed",
                "employee",
                employee.id,
                before={"status": before},
                after={"status": status},
            )
        return employee

    def change_rate(
        self, employee_id: str, rate: str | Decimal, valid_from: date, actor: int
    ) -> EmployeeRate:
        daily_rate = money(rate)
        try:
            with self.sessions() as session, session.begin():
                employee = session.get(Employee, employee_id)
                if employee is None:
                    raise NotFound("Сотрудник не найден.")
                if valid_from < employee.start_date:
                    raise DomainError("Дата ставки не может быть раньше начала работы.")
                existing = session.scalar(
                    select(EmployeeRate).where(
                        EmployeeRate.employee_id == employee_id,
                        EmployeeRate.valid_from == valid_from,
                    )
                )
                if existing:
                    raise Conflict("Ставка на эту дату уже существует.")
                row = EmployeeRate(
                    employee_id=employee_id,
                    valid_from=valid_from,
                    daily_rate=daily_rate,
                    currency=employee.currency,
                    created_by=actor,
                )
                session.add(row)
                session.flush()
                audit(
                    session,
                    actor,
                    "rate_changed",
                    "employee_rate",
                    row.id,
                    after={
                        "employee_id": employee_id,
                        "valid_from": valid_from.isoformat(),
                        "rate": str(daily_rate),
                    },
                )
            return row
        except IntegrityError as exc:
            raise Conflict("Ставка на эту дату уже существует.") from exc

    def list(self, *, active_only: bool = True) -> list[Employee]:
        with self.sessions() as session:
            query = select(Employee).order_by(Employee.name)
            if active_only:
                query = query.where(Employee.status == "active")
            return list(session.scalars(query))

    def get(self, employee_id: str) -> Employee:
        with self.sessions() as session:
            employee = session.get(Employee, employee_id)
            if employee is None:
                raise NotFound("Сотрудник не найден.")
            return employee

    def effective_rate(self, employee_id: str, on_date: date) -> EmployeeRate:
        with self.sessions() as session:
            row = session.scalar(
                select(EmployeeRate)
                .where(
                    EmployeeRate.employee_id == employee_id,
                    EmployeeRate.valid_from <= on_date,
                )
                .order_by(EmployeeRate.valid_from.desc())
                .limit(1)
            )
            if row is None:
                raise NotFound("Для сотрудника нет ставки на выбранную дату.")
            return row


class ObjectService:
    def __init__(self, sessions: sessionmaker[Session]):
        self.sessions = sessions

    def create(
        self,
        *,
        name: str,
        start_date: date,
        actor: int,
        address: str | None = None,
        description: str | None = None,
    ) -> WorkObject:
        name = clean_text(name, "Название объекта", 200, required=True)
        address = clean_text(address, "Адрес", 1000)
        description = clean_text(description, "Описание", 4000)
        with self.sessions() as session, session.begin():
            row = WorkObject(
                name=name,
                start_date=start_date,
                address=address,
                description=description,
            )
            session.add(row)
            session.flush()
            audit(session, actor, "object_created", "object", row.id, after={"name": name})
        return row

    def update(
        self,
        object_id: str,
        *,
        name: str,
        address: str | None,
        description: str | None,
        comment: str | None,
        actor: int,
        start_date: date | None = None,
    ) -> WorkObject:
        name = clean_text(name, "Название объекта", 200, required=True)
        address = clean_text(address, "Адрес", 1000)
        description = clean_text(description, "Описание", 4000)
        comment = clean_text(comment, "Комментарий", 4000)
        with self.sessions() as session, session.begin():
            row = session.get(WorkObject, object_id)
            if row is None:
                raise NotFound("Объект не найден.")
            if start_date is not None:
                first_shift = session.scalar(
                    select(func.min(Attendance.work_date)).where(Attendance.object_id == object_id)
                )
                if (first_shift and start_date > first_shift) or (
                    row.end_date and start_date > row.end_date
                ):
                    raise DomainError(
                        "Дата начала не может быть позже существующих смен или завершения объекта."
                    )
            before = {
                "name": row.name,
                "address": row.address,
                "description": row.description,
                "comment": row.comment,
                "start_date": row.start_date.isoformat(),
            }
            row.name = name
            row.address = address
            row.description = description
            row.comment = comment
            if start_date is not None:
                row.start_date = start_date
            audit(
                session,
                actor,
                "object_updated",
                "object",
                row.id,
                before=before,
                after={
                    "name": name,
                    "address": address,
                    "description": description,
                    "comment": comment,
                    "start_date": row.start_date.isoformat(),
                },
            )
        return row

    def set_status(self, object_id: str, status: str, *, actor: int) -> WorkObject:
        if status not in {"active", "completed", "archived"}:
            raise DomainError("Некорректный статус объекта.")
        with self.sessions() as session, session.begin():
            row = session.get(WorkObject, object_id)
            if row is None:
                raise NotFound("Объект не найден.")
            before = row.status
            row.status = status
            if status == "completed" and row.end_date is None:
                row.end_date = datetime.now(UTC).date()
            elif status == "active":
                row.end_date = None
            audit(
                session,
                actor,
                "object_status_changed",
                "object",
                row.id,
                before={"status": before},
                after={"status": status},
            )
        return row

    def list(self, *, active_only: bool = True) -> list[WorkObject]:
        with self.sessions() as session:
            query = select(WorkObject).order_by(WorkObject.name)
            if active_only:
                query = query.where(WorkObject.status == "active")
            return list(session.scalars(query))

    def get(self, object_id: str) -> WorkObject:
        with self.sessions() as session:
            row = session.get(WorkObject, object_id)
            if row is None:
                raise NotFound("Объект не найден.")
            return row


class AttendanceService:
    def __init__(self, sessions: sessionmaker[Session]):
        self.sessions = sessions

    @staticmethod
    def _coefficients(
        employee_ids: list[str], coefficient: str | Decimal | dict[str, str | Decimal]
    ) -> dict[str, Decimal]:
        if isinstance(coefficient, dict):
            if set(coefficient) != set(employee_ids):
                raise DomainError("Коэффициент должен быть указан для каждого сотрудника.")
            return {
                employee_id: day_coefficient(coefficient[employee_id])
                for employee_id in employee_ids
            }
        value = day_coefficient(coefficient)
        return dict.fromkeys(employee_ids, value)

    @staticmethod
    def _rate(
        session: Session,
        employee_id: str,
        work_date: date,
        object_id: str,
        require_assignment: bool,
    ) -> tuple[Decimal | None, str]:
        member = session.scalar(
            select(ObjectEmployee).where(
                ObjectEmployee.employee_id == employee_id, ObjectEmployee.object_id == object_id
            )
        )
        if member is not None:
            if not member.active:
                raise DomainError(
                    "Сотрудник убран из состава объекта. Сначала верните его в состав."
                )
            employee = session.get(Employee, employee_id)
            if work_date < employee.start_date:
                raise DomainError("Смена не может быть раньше начала работы сотрудника.")
            if member.shift_rate is not None:
                return member.shift_rate, employee.currency
            base_rate = session.scalar(
                select(EmployeeRate)
                .where(
                    EmployeeRate.employee_id == employee_id,
                    EmployeeRate.valid_from <= work_date,
                )
                .order_by(EmployeeRate.valid_from.desc())
                .limit(1)
            )
            return (
                (base_rate.daily_rate, base_rate.currency)
                if base_rate is not None
                else (None, employee.currency)
            )
        if require_assignment:
            raise DomainError("Сначала добавьте сотрудника в состав объекта.")
        # Legacy payroll API: retained for historical integrations without object rosters.
        row = session.scalar(
            select(EmployeeRate)
            .where(
                EmployeeRate.employee_id == employee_id,
                EmployeeRate.valid_from <= work_date,
            )
            .order_by(EmployeeRate.valid_from.desc())
            .limit(1)
        )
        if row is None:
            raise NotFound("Для сотрудника нет ставки на выбранную дату.")
        return row.daily_rate, row.currency

    def preview(
        self,
        *,
        employee_ids: Iterable[str],
        object_id: str,
        work_date: date,
        coefficient: str | Decimal | dict[str, str | Decimal],
        require_assignment: bool = False,
    ) -> list[AttendancePreview]:
        ids = list(dict.fromkeys(employee_ids))
        if not ids:
            raise DomainError("Выберите хотя бы одного сотрудника.")
        coefficients = self._coefficients(ids, coefficient)
        with self.sessions() as session:
            obj = session.get(WorkObject, object_id)
            if obj is None or obj.status != "active":
                raise NotFound("Активный объект не найден.")
            if work_date < obj.start_date:
                raise DomainError("Рабочий день не может быть раньше начала объекта.")
            duplicate = session.scalar(
                select(Attendance.id)
                .where(
                    Attendance.employee_id.in_(ids),
                    Attendance.object_id == object_id,
                    Attendance.work_date == work_date,
                    Attendance.voided_at.is_(None),
                )
                .limit(1)
            )
            if duplicate:
                raise Conflict("Рабочий день уже существует. Сначала проверьте историю.")
            result = []
            for employee_id in ids:
                employee = session.get(Employee, employee_id)
                if employee is None or employee.status != "active":
                    raise NotFound("Активный сотрудник не найден.")
                rate, currency = self._rate(
                    session, employee_id, work_date, object_id, require_assignment
                )
                employee_coefficient = coefficients[employee_id]
                earned = as_money(employee_coefficient * rate) if rate is not None else None
                result.append(
                    AttendancePreview(
                        employee_id=employee.id,
                        employee_name=employee.name,
                        coefficient=employee_coefficient,
                        rate=rate,
                        earned=earned,
                        currency=currency,
                    )
                )
            return result

    def create_bulk(
        self,
        *,
        employee_ids: Iterable[str],
        object_id: str,
        work_date: date,
        coefficient: str | Decimal | dict[str, str | Decimal],
        actor: int,
        operation_key: str,
        comment: str | None = None,
        require_assignment: bool = False,
    ) -> list[Attendance]:
        ids = list(dict.fromkeys(employee_ids))
        if not ids:
            raise DomainError("Выберите хотя бы одного сотрудника.")
        coefficients = self._coefficients(ids, coefficient)
        comment = clean_text(comment, "Комментарий", 4000)
        operation_key = clean_text(operation_key, "Ключ операции", 110, required=True)
        keys = {employee_id: f"{operation_key}:{employee_id}" for employee_id in ids}
        try:
            with self.sessions() as session, session.begin():
                existing = list(
                    session.scalars(
                        select(Attendance).where(Attendance.idempotency_key.in_(keys.values()))
                    )
                )
                if len(existing) == len(ids):
                    expected_ids = set(ids)
                    if any(
                        row.employee_id not in expected_ids
                        or row.object_id != object_id
                        or row.work_date != work_date
                        or row.coefficient != coefficients[row.employee_id]
                        for row in existing
                    ):
                        raise Conflict("Ключ повтора уже использован другой операцией.")
                    return existing
                if existing:
                    raise Conflict("Операция была сохранена частично; нужна проверка.")
                obj = session.get(WorkObject, object_id)
                if obj is None or obj.status != "active":
                    raise NotFound("Активный объект не найден.")
                if work_date < obj.start_date:
                    raise DomainError("Рабочий день не может быть раньше начала объекта.")
                duplicate = session.scalar(
                    select(Attendance.id)
                    .where(
                        Attendance.employee_id.in_(ids),
                        Attendance.object_id == object_id,
                        Attendance.work_date == work_date,
                        Attendance.voided_at.is_(None),
                    )
                    .limit(1)
                )
                if duplicate:
                    raise Conflict("Рабочий день уже существует. Сначала проверьте историю.")
                rows = []
                for employee_id in ids:
                    employee = session.get(Employee, employee_id)
                    if employee is None or employee.status != "active":
                        raise NotFound("Активный сотрудник не найден.")
                    rate, currency = self._rate(
                        session, employee_id, work_date, object_id, require_assignment
                    )
                    employee_coefficient = coefficients[employee_id]
                    row = Attendance(
                        employee_id=employee_id,
                        object_id=object_id,
                        work_date=work_date,
                        coefficient=employee_coefficient,
                        rate_snapshot=rate,
                        currency=currency,
                        earned_amount=as_money(employee_coefficient * rate)
                        if rate is not None
                        else None,
                        comment=comment,
                        idempotency_key=keys[employee_id],
                        created_by=actor,
                    )
                    session.add(row)
                    session.flush()
                    audit(
                        session,
                        actor,
                        "attendance_created",
                        "attendance",
                        row.id,
                        after={
                            "employee_id": employee_id,
                            "object_id": object_id,
                            "date": work_date.isoformat(),
                            "coefficient": str(employee_coefficient),
                            "rate": str(row.rate_snapshot),
                            "earned": str(row.earned_amount),
                        },
                    )
                    rows.append(row)
                return rows
        except IntegrityError as exc:
            with self.sessions() as session:
                existing = list(
                    session.scalars(
                        select(Attendance).where(Attendance.idempotency_key.in_(keys.values()))
                    )
                )
                if len(existing) == len(ids) and all(
                    row.employee_id in coefficients
                    and row.object_id == object_id
                    and row.work_date == work_date
                    and row.coefficient == coefficients[row.employee_id]
                    for row in existing
                ):
                    return existing
            raise Conflict("Рабочий день уже сохранён.") from exc

    def price_unrated(self, attendance_id: str, rate, *, actor: int):
        value = money(rate)
        with self.sessions() as session, session.begin():
            row = session.get(Attendance, attendance_id)
            if row is None:
                raise NotFound("Смена не найдена.")
            earned = as_money(row.coefficient * value)
            changed = session.scalar(
                update(Attendance)
                .where(
                    Attendance.id == attendance_id,
                    Attendance.voided_at.is_(None),
                    Attendance.rate_snapshot.is_(None),
                    Attendance.earned_amount.is_(None),
                )
                .values(rate_snapshot=value, earned_amount=earned, modified_by=actor)
                .returning(Attendance.id)
            )
            if changed is None:
                raise Conflict("Смена уже рассчитана или отменена. Обновите историю.")
            audit(
                session,
                actor,
                "attendance_priced",
                "attendance",
                row.id,
                before={"rate": None, "earned": None},
                after={"rate": str(value), "earned": str(earned)},
            )

    def void(self, attendance_id: str, *, actor: int, reason: str) -> None:
        reason = clean_text(reason, "Причина отмены", 1000, required=True)
        with self.sessions() as session, session.begin():
            row = session.get(Attendance, attendance_id)
            if row is None:
                raise NotFound("Рабочий день не найден.")
            if row.voided_at:
                return
            before = {"earned": str(row.earned_amount), "date": row.work_date.isoformat()}
            row.voided_at = datetime.now(UTC)
            row.voided_by = actor
            row.void_reason = reason
            row.modified_by = actor
            audit(
                session,
                actor,
                "attendance_voided",
                "attendance",
                row.id,
                before=before,
                after={"reason": reason},
            )

    def get(self, attendance_id: str) -> Attendance:
        with self.sessions() as session:
            row = session.get(Attendance, attendance_id)
            if row is None:
                raise NotFound("Рабочий день не найден.")
            return row


class PayrollService:
    def __init__(self, sessions: sessionmaker[Session]):
        self.sessions = sessions

    def summary(
        self,
        employee_id: str,
        *,
        object_id: str | None = None,
        date_from: date | None = None,
        date_to: date | None = None,
    ) -> PayrollSummary:
        with self.sessions() as session:
            employee = session.get(Employee, employee_id)
            if employee is None:
                raise NotFound("Сотрудник не найден.")
            attendance_filters = [
                Attendance.employee_id == employee_id,
                Attendance.voided_at.is_(None),
            ]
            payment_filters = [Payment.employee_id == employee_id, Payment.voided_at.is_(None)]
            if object_id is not None:
                attendance_filters.append(Attendance.object_id == object_id)
                payment_filters.append(Payment.object_id == object_id)
            if date_from:
                attendance_filters.append(Attendance.work_date >= date_from)
                payment_filters.append(Payment.payment_date >= date_from)
            if date_to:
                attendance_filters.append(Attendance.work_date <= date_to)
                payment_filters.append(Payment.payment_date <= date_to)
            days, earned, unrated = session.execute(
                select(
                    func.coalesce(func.sum(Attendance.coefficient), 0),
                    func.coalesce(func.sum(Attendance.earned_amount), 0),
                    func.count() - func.count(Attendance.earned_amount),
                ).where(*attendance_filters)
            ).one()
            paid = session.scalar(
                select(func.coalesce(func.sum(Payment.amount), 0)).where(*payment_filters)
            )
            return PayrollSummary(
                employee_id=employee.id,
                employee_name=employee.name,
                currency=employee.currency,
                days=Decimal(days or 0),
                earned=as_money(earned),
                paid=as_money(paid),
                unrated_shifts=unrated,
            )


class PaymentService:
    METHODS: ClassVar[frozenset[str]] = frozenset({"cash", "bank", "other"})

    def __init__(self, sessions: sessionmaker[Session]):
        self.sessions = sessions

    def create(
        self,
        *,
        employee_id: str,
        amount: str | Decimal,
        payment_date: date,
        method: str,
        actor: int,
        idempotency_key: str,
        object_id: str | None = None,
        comment: str | None = None,
    ) -> Payment:
        amount = money(amount)
        comment = clean_text(comment, "Комментарий", 4000)
        idempotency_key = clean_text(idempotency_key, "Ключ операции", 150, required=True)
        if method not in self.METHODS:
            raise DomainError("Неизвестный способ выплаты.")
        try:
            with self.sessions() as session, session.begin():
                existing = session.scalar(
                    select(Payment).where(Payment.idempotency_key == idempotency_key)
                )
                if existing:
                    if (
                        existing.employee_id != employee_id
                        or existing.object_id != object_id
                        or existing.payment_date != payment_date
                        or existing.amount != amount
                        or existing.method != method
                    ):
                        raise Conflict("Ключ повтора уже использован другой выплатой.")
                    return existing
                employee = session.get(Employee, employee_id)
                if employee is None:
                    raise NotFound("Сотрудник не найден.")
                if object_id and session.get(WorkObject, object_id) is None:
                    raise NotFound("Объект не найден.")
                row = Payment(
                    employee_id=employee_id,
                    object_id=object_id,
                    payment_date=payment_date,
                    amount=amount,
                    currency=employee.currency,
                    method=method,
                    comment=comment,
                    created_by=actor,
                    idempotency_key=idempotency_key,
                )
                session.add(row)
                session.flush()
                audit(
                    session,
                    actor,
                    "payment_created",
                    "payment",
                    row.id,
                    after={
                        "employee_id": employee_id,
                        "amount": str(amount),
                        "currency": employee.currency,
                        "date": payment_date.isoformat(),
                    },
                )
                return row
        except IntegrityError as exc:
            with self.sessions() as session:
                existing = session.scalar(
                    select(Payment).where(Payment.idempotency_key == idempotency_key)
                )
                if existing and (
                    existing.employee_id == employee_id
                    and existing.object_id == object_id
                    and existing.payment_date == payment_date
                    and existing.amount == amount
                    and existing.method == method
                ):
                    return existing
            raise Conflict("Выплата уже сохранена.") from exc

    def void(self, payment_id: str, *, actor: int, reason: str) -> None:
        reason = clean_text(reason, "Причина отмены", 1000, required=True)
        with self.sessions() as session, session.begin():
            row = session.get(Payment, payment_id)
            if row is None:
                raise NotFound("Выплата не найдена.")
            if row.voided_at:
                return
            before = {"amount": str(row.amount), "date": row.payment_date.isoformat()}
            row.voided_at = datetime.now(UTC)
            row.voided_by = actor
            row.void_reason = reason
            row.modified_by = actor
            audit(
                session,
                actor,
                "payment_voided",
                "payment",
                row.id,
                before=before,
                after={"reason": reason},
            )

    def get(self, payment_id: str) -> Payment:
        with self.sessions() as session:
            row = session.get(Payment, payment_id)
            if row is None:
                raise NotFound("Выплата не найдена.")
            return row

    def history(self, employee_id: str, object_id: str, *, offset=0):
        with self.sessions() as session:
            return list(
                session.scalars(
                    select(Payment)
                    .where(
                        Payment.employee_id == employee_id,
                        Payment.object_id == object_id,
                        Payment.voided_at.is_(None),
                    )
                    .order_by(Payment.payment_date.desc(), Payment.created_at.desc(), Payment.id)
                    .offset(offset)
                    .limit(20)
                )
            )


class AccessService:
    def __init__(self, sessions: sessionmaker[Session], owner_ids: set[int]):
        self.sessions = sessions
        self.owner_ids = owner_ids

    def ensure_owner(self, telegram_id: int) -> bool:
        if telegram_id not in self.owner_ids:
            return False
        with self.sessions() as session, session.begin():
            user = session.get(User, telegram_id)
            if user is None:
                session.add(User(telegram_id=telegram_id, role="OWNER"))
            elif not user.is_active or user.role != "OWNER":
                return False
        return True


class UpdateDedupService:
    STALE_AFTER = timedelta(minutes=10)

    def __init__(self, sessions: sessionmaker[Session]):
        self.sessions = sessions

    def claim(self, update_id: int) -> bool:
        now = datetime.now(UTC)
        try:
            with self.sessions() as session, session.begin():
                row = session.get(TelegramUpdate, update_id)
                if row is None:
                    session.add(TelegramUpdate(update_id=update_id, claimed_at=now))
                    return True
                claimed_at = row.claimed_at
                if claimed_at.tzinfo is None:
                    claimed_at = claimed_at.replace(tzinfo=UTC)
                if row.status == "done" or now - claimed_at < self.STALE_AFTER:
                    return False
                claimed = session.execute(
                    update(TelegramUpdate)
                    .where(
                        TelegramUpdate.update_id == update_id,
                        TelegramUpdate.status != "done",
                        TelegramUpdate.claimed_at == row.claimed_at,
                    )
                    .values(status="processing", claimed_at=now)
                )
                return claimed.rowcount == 1
        except IntegrityError:
            return False

    def done(self, update_id: int) -> None:
        with self.sessions() as session, session.begin():
            row = session.get(TelegramUpdate, update_id)
            if row:
                row.status = "done"
                row.processed_at = datetime.now(UTC)
