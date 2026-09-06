from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest
from sqlalchemy import func, select

from app.models import Attendance, AuditLog, Employee, ObjectEmployee, Payment, WorkObject
from app.services import Conflict
from app.teams import TeamService

ACTOR = 1001


def setup_employee_object(services):
    employee = services["employees"].create(
        name="Иван Петров", start_date=date(2026, 9, 1), actor=ACTOR
    )
    obj = services["objects"].create(
        name="ЖК Северный", start_date=date(2026, 9, 1), actor=ACTOR
    )
    member = TeamService(services["sessions"]).add_many(
        obj.id, [employee.id], actor=ACTOR
    )[0]
    TeamService(services["sessions"]).set_rate(member.id, "150", actor=ACTOR)
    return employee, obj


def add_shift_and_payment(services, employee, obj):
    services["attendance"].create_bulk(
        employee_ids=[employee.id],
        object_id=obj.id,
        work_date=date(2026, 9, 4),
        actor=ACTOR,
        operation_key=f"delete-shift:{employee.id}:{obj.id}",
    )
    services["payments"].create(
        employee_id=employee.id,
        object_id=obj.id,
        amount="100",
        payment_date=date(2026, 9, 4),
        actor=ACTOR,
        idempotency_key=f"delete-payment:{employee.id}:{obj.id}",
    )


def test_employee_delete_removes_card_and_all_related_records(services):
    employee, obj = setup_employee_object(services)
    add_shift_and_payment(services, employee, obj)

    services["employees"].delete(employee.id, actor=ACTOR)

    with services["sessions"]() as session:
        assert session.get(Employee, employee.id) is None
        assert session.get(WorkObject, obj.id) is not None
        assert session.scalar(select(func.count()).select_from(ObjectEmployee)) == 0
        assert session.scalar(select(func.count()).select_from(Attendance)) == 0
        assert session.scalar(select(func.count()).select_from(Payment)) == 0
        assert session.scalar(
            select(func.count()).select_from(AuditLog).where(AuditLog.action == "employee_deleted")
        ) == 1


def test_object_delete_removes_object_ledger_but_keeps_employee(services):
    employee, obj = setup_employee_object(services)
    add_shift_and_payment(services, employee, obj)

    services["objects"].delete(obj.id, actor=ACTOR)

    with services["sessions"]() as session:
        assert session.get(WorkObject, obj.id) is None
        assert session.get(Employee, employee.id) is not None
        assert session.scalar(select(func.count()).select_from(ObjectEmployee)) == 0
        assert session.scalar(select(func.count()).select_from(Attendance)) == 0
        assert session.scalar(select(func.count()).select_from(Payment)) == 0
        assert session.scalar(
            select(func.count()).select_from(AuditLog).where(AuditLog.action == "object_deleted")
        ) == 1


def test_object_rate_change_never_recalculates_old_shift(services):
    employee, obj = setup_employee_object(services)
    team = TeamService(services["sessions"])
    old = services["attendance"].create_bulk(
        employee_ids=[employee.id],
        object_id=obj.id,
        work_date=date(2026, 9, 10),
        actor=ACTOR,
        operation_key="old-day",
    )[0]
    team.set_rate(team.roster(obj.id)[0][0].id, "175", actor=ACTOR)
    new = services["attendance"].create_bulk(
        employee_ids=[employee.id],
        object_id=obj.id,
        work_date=date(2026, 9, 11),
        actor=ACTOR,
        operation_key="new-day",
    )[0]
    assert old.rate_snapshot == old.earned_amount == Decimal(150)
    assert new.rate_snapshot == new.earned_amount == Decimal(175)
    assert services["payroll"].summary(employee.id, object_id=obj.id).earned == Decimal(325)


def test_bulk_attendance_is_atomic_idempotent_and_unique(services):
    first, obj = setup_employee_object(services)
    second = services["employees"].create(
        name="Сергей", start_date=date(2026, 9, 1), actor=ACTOR
    )
    team = TeamService(services["sessions"])
    second_member = team.add_many(obj.id, [second.id], actor=ACTOR)[0]
    team.set_rate(second_member.id, "170", actor=ACTOR)
    args = {
        "employee_ids": [first.id, second.id],
        "object_id": obj.id,
        "work_date": date(2026, 9, 4),
        "actor": ACTOR,
        "operation_key": "telegram-update-42",
    }
    original = services["attendance"].create_bulk(**args)
    retry = services["attendance"].create_bulk(**args)
    assert {row.id for row in retry} == {row.id for row in original}
    with pytest.raises(Conflict):
        services["attendance"].preview(
            employee_ids=[first.id], object_id=obj.id, work_date=date(2026, 9, 4)
        )
    with pytest.raises(Conflict):
        services["attendance"].create_bulk(**(args | {"operation_key": "different"}))
    with services["sessions"]() as session:
        assert session.scalar(select(func.count()).select_from(Attendance)) == 2


def test_object_payment_changes_balance_and_retry_is_safe(services):
    employee, obj = setup_employee_object(services)
    services["attendance"].create_bulk(
        employee_ids=[employee.id],
        object_id=obj.id,
        work_date=date(2026, 9, 4),
        actor=ACTOR,
        operation_key="day-1",
    )
    args = {
        "employee_id": employee.id,
        "object_id": obj.id,
        "amount": "100",
        "payment_date": date(2026, 9, 4),
        "actor": ACTOR,
        "idempotency_key": "payment-update-9",
    }
    payment = services["payments"].create(**args)
    assert services["payments"].create(**args).id == payment.id
    with pytest.raises(Conflict):
        services["payments"].create(**(args | {"amount": "90"}))
    summary = services["payroll"].summary(employee.id, object_id=obj.id)
    assert (summary.earned, summary.paid, summary.balance) == (
        Decimal(150),
        Decimal(100),
        Decimal(50),
    )


def test_void_keeps_rows_and_audit_trail(services):
    employee, obj = setup_employee_object(services)
    attendance = services["attendance"].create_bulk(
        employee_ids=[employee.id],
        object_id=obj.id,
        work_date=date(2026, 9, 4),
        actor=ACTOR,
        operation_key="day",
    )[0]
    payment = services["payments"].create(
        employee_id=employee.id,
        object_id=obj.id,
        amount="50",
        payment_date=date(2026, 9, 4),
        actor=ACTOR,
        idempotency_key="payment",
    )
    services["attendance"].void(attendance.id, actor=ACTOR, reason="Ошибка даты")
    services["payments"].void(payment.id, actor=ACTOR, reason="Ошибка суммы")
    summary = services["payroll"].summary(employee.id, object_id=obj.id)
    assert summary.earned == summary.paid == summary.balance == Decimal(0)
    with services["sessions"]() as session:
        assert session.get(Attendance, attendance.id).voided_at is not None
        assert session.get(Payment, payment.id).voided_at is not None
        assert {"attendance_voided", "payment_voided"} <= set(
            session.scalars(select(AuditLog.action))
        )


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
    services["objects"].update(
        obj.id,
        name="ЖК Северный 2",
        address="Улица 1",
        description="Ремонт",
        comment="Приоритет",
        actor=ACTOR,
    )
    with services["sessions"]() as session:
        actions = set(session.scalars(select(AuditLog.action)))
    assert {"employee_updated", "object_updated"} <= actions


def test_state_survives_new_session(services):
    employee, obj = setup_employee_object(services)
    services["attendance"].create_bulk(
        employee_ids=[employee.id],
        object_id=obj.id,
        work_date=date(2026, 9, 4),
        actor=ACTOR,
        operation_key="restart-day",
    )
    services["sessions"].kw["bind"].dispose()
    assert services["payroll"].summary(employee.id, object_id=obj.id).earned == Decimal(150)
