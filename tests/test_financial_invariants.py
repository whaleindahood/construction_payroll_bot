from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest
from sqlalchemy import func, select

from app.models import Attendance, AuditLog, Payment
from app.services import Conflict

ACTOR = 1001


def setup_employee_object(services):
    employee = services["employees"].create(
        name="Иван Петров",
        rate="150.00",
        currency="EUR",
        start_date=date(2026, 9, 1),
        actor=ACTOR,
    )
    obj = services["objects"].create(
        name="Amsterdam House",
        start_date=date(2026, 9, 1),
        actor=ACTOR,
    )
    return employee, obj


def test_rate_change_never_recalculates_historical_earnings(services):
    employee, obj = setup_employee_object(services)
    old = services["attendance"].create_bulk(
        employee_ids=[employee.id],
        object_id=obj.id,
        work_date=date(2026, 9, 10),
        coefficient="1",
        actor=ACTOR,
        operation_key="old-day",
    )[0]
    services["employees"].change_rate(employee.id, "175", date(2026, 9, 16), ACTOR)
    new = services["attendance"].create_bulk(
        employee_ids=[employee.id],
        object_id=obj.id,
        work_date=date(2026, 9, 20),
        coefficient="0.5",
        actor=ACTOR,
        operation_key="new-day",
    )[0]

    assert old.rate_snapshot == Decimal("150.00")
    assert old.earned_amount == Decimal("150.00")
    assert new.rate_snapshot == Decimal("175.00")
    assert new.earned_amount == Decimal("87.50")
    assert services["payroll"].summary(employee.id).earned == Decimal("237.50")


def test_bulk_attendance_is_atomic_idempotent_and_unique(services):
    first, obj = setup_employee_object(services)
    second = services["employees"].create(
        name="Сергей",
        rate="170",
        currency="EUR",
        start_date=date(2026, 9, 1),
        actor=ACTOR,
    )
    args = {
        "employee_ids": [first.id, second.id],
        "object_id": obj.id,
        "work_date": date(2026, 9, 4),
        "coefficient": "1",
        "actor": ACTOR,
        "operation_key": "telegram-update-42",
    }

    original = services["attendance"].create_bulk(**args)
    retry = services["attendance"].create_bulk(**args)

    assert {row.id for row in retry} == {row.id for row in original}
    with pytest.raises(Conflict):
        services["attendance"].preview(
            employee_ids=[first.id],
            object_id=obj.id,
            work_date=date(2026, 9, 4),
            coefficient="1",
        )
    with services["sessions"]() as session:
        assert session.scalar(select(func.count()).select_from(Attendance)) == 2
    with pytest.raises(Conflict):
        services["attendance"].create_bulk(**(args | {"operation_key": "different"}))
    with pytest.raises(Conflict):
        services["attendance"].create_bulk(**(args | {"coefficient": "0.5"}))

    individual = services["attendance"].create_bulk(
        **(
            args
            | {
                "work_date": date(2026, 9, 5),
                "coefficient": {first.id: "0.5", second.id: "1.5"},
                "operation_key": "individual-coefficients",
            }
        )
    )
    assert {row.employee_id: row.earned_amount for row in individual} == {
        first.id: Decimal("75.00"),
        second.id: Decimal("255.00"),
    }


def test_partial_payment_changes_derived_balance_and_retry_is_safe(services):
    employee, obj = setup_employee_object(services)
    services["attendance"].create_bulk(
        employee_ids=[employee.id],
        object_id=obj.id,
        work_date=date(2026, 9, 4),
        coefficient="1",
        actor=ACTOR,
        operation_key="day-1",
    )
    payment = services["payments"].create(
        employee_id=employee.id,
        amount="100",
        payment_date=date(2026, 9, 4),
        method="bank",
        actor=ACTOR,
        idempotency_key="payment-update-9",
    )
    retry = services["payments"].create(
        employee_id=employee.id,
        amount="100",
        payment_date=date(2026, 9, 4),
        method="bank",
        actor=ACTOR,
        idempotency_key="payment-update-9",
    )

    assert retry.id == payment.id
    with pytest.raises(Conflict):
        services["payments"].create(
            employee_id=employee.id,
            amount="90",
            payment_date=date(2026, 9, 4),
            method="bank",
            actor=ACTOR,
            idempotency_key="payment-update-9",
        )
    summary = services["payroll"].summary(employee.id)
    assert summary.earned == Decimal("150.00")
    assert summary.paid == Decimal("100.00")
    assert summary.balance == Decimal("50.00")
    with services["sessions"]() as session:
        assert session.scalar(select(func.count()).select_from(Payment)) == 1


def test_void_keeps_financial_rows_and_audit_trail(services):
    employee, obj = setup_employee_object(services)
    attendance = services["attendance"].create_bulk(
        employee_ids=[employee.id],
        object_id=obj.id,
        work_date=date(2026, 9, 4),
        coefficient="1",
        actor=ACTOR,
        operation_key="day",
    )[0]
    payment = services["payments"].create(
        employee_id=employee.id,
        amount="50",
        payment_date=date(2026, 9, 4),
        method="cash",
        actor=ACTOR,
        idempotency_key="payment",
    )

    services["attendance"].void(attendance.id, actor=ACTOR, reason="Ошибка даты")
    services["payments"].void(payment.id, actor=ACTOR, reason="Ошибка суммы")

    summary = services["payroll"].summary(employee.id)
    assert summary.earned == summary.paid == summary.balance == Decimal("0.00")
    with services["sessions"]() as session:
        assert session.get(Attendance, attendance.id).voided_at is not None
        assert session.get(Payment, payment.id).voided_at is not None
        actions = set(session.scalars(select(AuditLog.action)))
        assert {"attendance_voided", "payment_voided"} <= actions

    replacement = services["attendance"].create_bulk(
        employee_ids=[employee.id],
        object_id=obj.id,
        work_date=date(2026, 9, 4),
        coefficient="0.5",
        actor=ACTOR,
        operation_key="corrected-day",
    )[0]
    assert replacement.id != attendance.id
    assert services["payroll"].summary(employee.id).earned == Decimal("75.00")


def test_currencies_are_kept_in_separate_employee_ledgers(services):
    setup_employee_object(services)

    employee = services["employees"].create(
        name="Другой сотрудник",
        rate="100",
        currency="RUB",
        start_date=date(2026, 9, 1),
        actor=ACTOR,
    )

    assert services["payroll"].summary(employee.id).currency == "RUB"


def test_employee_and_object_edits_are_audited(services):
    employee, obj = setup_employee_object(services)

    services["employees"].update(
        employee.id,
        name="Иван Петрович",
        phone="+79990000000",
        telegram_id=555,
        comment="Бригадир",
        actor=ACTOR,
    )
    services["employees"].set_status(employee.id, "inactive", actor=ACTOR)
    services["objects"].update(
        obj.id,
        name="Amsterdam House 2",
        address="Street 1",
        description="Renovation",
        comment="Priority",
        actor=ACTOR,
    )
    services["objects"].set_status(obj.id, "completed", actor=ACTOR)

    assert services["employees"].get(employee.id).status == "inactive"
    assert services["objects"].get(obj.id).status == "completed"
    with services["sessions"]() as session:
        actions = set(session.scalars(select(AuditLog.action)))
    assert {
        "employee_updated",
        "employee_status_changed",
        "object_updated",
        "object_status_changed",
    } <= actions


def test_state_survives_new_session_factory(services, tmp_path):
    employee, obj = setup_employee_object(services)
    services["attendance"].create_bulk(
        employee_ids=[employee.id],
        object_id=obj.id,
        work_date=date(2026, 9, 4),
        coefficient="1.5",
        actor=ACTOR,
        operation_key="restart-day",
    )
    engine = services["sessions"].kw["bind"]
    engine.dispose()

    assert services["payroll"].summary(employee.id).earned == Decimal("225.00")
