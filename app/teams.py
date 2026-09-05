from __future__ import annotations

from datetime import date

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError

from app.models import Attendance, Employee, ObjectEmployee, WorkObject
from app.services import Conflict, NotFound, audit, money


class TeamService:
    def __init__(self, sessions):
        self.sessions = sessions

    def add(self, object_id: str, employee_id: str, *, shift_rate, actor: int):
        rate = money(shift_rate) if shift_rate is not None else None
        try:
            with self.sessions() as session, session.begin():
                obj = session.get(WorkObject, object_id)
                employee = session.get(Employee, employee_id)
                if obj is None or obj.status != "active":
                    raise NotFound("Активный объект не найден.")
                if employee is None or employee.status != "active":
                    raise NotFound("Активный сотрудник не найден.")
                row = session.scalar(
                    select(ObjectEmployee).where(
                        ObjectEmployee.object_id == object_id,
                        ObjectEmployee.employee_id == employee_id,
                    )
                )
                if row is not None and row.active:
                    raise Conflict("Сотрудник уже добавлен на этот объект.")
                restored = row is not None
                if row is None:
                    row = ObjectEmployee(object_id=object_id, employee_id=employee_id)
                    session.add(row)
                row.active = True
                row.shift_rate = rate
                session.flush()
                audit(
                    session,
                    actor,
                    "employee_returned_to_object" if restored else "employee_added_to_object",
                    "object_employee",
                    row.id,
                    after={"object_id": object_id, "employee_id": employee_id},
                )
            return row
        except IntegrityError as exc:
            raise Conflict("Сотрудник уже добавлен на этот объект.") from exc

    def get(self, member_id: str):
        with self.sessions() as session:
            member = session.get(ObjectEmployee, member_id)
            if member is None:
                raise NotFound("Сотрудник не найден в составе объекта.")
            return member

    def set_rate(self, member_id: str, rate, *, actor: int):
        value = money(rate) if rate is not None else None
        with self.sessions() as session, session.begin():
            member = session.get(ObjectEmployee, member_id)
            if member is None:
                raise NotFound("Сотрудник не найден в составе объекта.")
            before = member.shift_rate
            member.shift_rate = value
            audit(
                session,
                actor,
                "object_shift_rate_changed",
                "object_employee",
                member.id,
                before={"rate": str(before)},
                after={"rate": str(value)},
            )

    def set_active(self, member_id: str, active: bool, *, actor: int):
        with self.sessions() as session, session.begin():
            member = session.get(ObjectEmployee, member_id)
            if member is None:
                raise NotFound("Сотрудник не найден в составе объекта.")
            if active:
                obj = session.get(WorkObject, member.object_id)
                employee = session.get(Employee, member.employee_id)
                if obj.status != "active" or employee.status != "active":
                    raise Conflict("Сначала восстановите объект и карточку сотрудника.")
            if member.active == active:
                return
            member.active = active
            audit(
                session,
                actor,
                "object_membership_changed",
                "object_employee",
                member.id,
                before={"active": not active},
                after={"active": active},
            )

    def available(self, object_id: str):
        with self.sessions() as session:
            assigned = select(ObjectEmployee.employee_id).where(
                ObjectEmployee.object_id == object_id, ObjectEmployee.active.is_(True)
            )
            return list(
                session.scalars(
                    select(Employee)
                    .where(Employee.status == "active", Employee.id.not_in(assigned))
                    .order_by(Employee.name)
                )
            )

    def roster(self, object_id: str, *, active_only=False):
        with self.sessions() as session:
            counts = (
                select(Attendance.employee_id, func.count().label("shifts"))
                .where(Attendance.object_id == object_id, Attendance.voided_at.is_(None))
                .group_by(Attendance.employee_id)
                .subquery()
            )
            query = (
                select(ObjectEmployee, Employee, func.coalesce(counts.c.shifts, 0))
                .join(Employee, Employee.id == ObjectEmployee.employee_id)
                .outerjoin(counts, counts.c.employee_id == Employee.id)
                .where(ObjectEmployee.object_id == object_id)
                .order_by(Employee.name)
            )
            if active_only:
                query = query.where(ObjectEmployee.active.is_(True), Employee.status == "active")
            return session.execute(query).all()

    def employee_objects(self, employee_id: str):
        with self.sessions() as session:
            counts = (
                select(Attendance.object_id, func.count().label("shifts"))
                .where(Attendance.employee_id == employee_id, Attendance.voided_at.is_(None))
                .group_by(Attendance.object_id)
                .subquery()
            )
            return session.execute(
                select(WorkObject, func.coalesce(counts.c.shifts, 0))
                .join(ObjectEmployee, ObjectEmployee.object_id == WorkObject.id)
                .outerjoin(counts, counts.c.object_id == WorkObject.id)
                .where(ObjectEmployee.employee_id == employee_id)
                .order_by(WorkObject.name)
            ).all()

    def day(self, object_id: str, work_date: date):
        with self.sessions() as session:
            return list(
                session.scalars(
                    select(Attendance).where(
                        Attendance.object_id == object_id,
                        Attendance.work_date == work_date,
                        Attendance.voided_at.is_(None),
                    )
                )
            )

    def history(self, object_id: str, employee_id: str, *, offset=0):
        with self.sessions() as session:
            return list(
                session.scalars(
                    select(Attendance)
                    .where(
                        Attendance.object_id == object_id,
                        Attendance.employee_id == employee_id,
                        Attendance.voided_at.is_(None),
                    )
                    .order_by(Attendance.work_date.desc())
                    .offset(offset)
                    .limit(20)
                )
            )
