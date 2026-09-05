from __future__ import annotations

import asyncio
from datetime import UTC, date, datetime
from decimal import Decimal

import pytest
from sqlalchemy import func, select

from app.config import Settings
from app.db import create_schema, make_session_factory
from app.main import compose
from app.models import Attendance, Employee, ObjectEmployee, WorkObject
from app.services import AttendanceService, Conflict, DomainError, EmployeeService
from app.teams import TeamService
from tests.test_bot_end_to_end import FakeBot, telegram_message
from tests.test_employee_self_service import telegram_callback


def test_shifts_and_rates_are_separate_for_each_object(services):
    team = TeamService(services["sessions"])
    employee = services["employees"].create(
        name="Иван", currency="RUB", start_date=date(2026, 1, 1), actor=1001
    )
    first, second = [
        services["objects"].create(name=name, start_date=date(2026, 1, 1), actor=1001)
        for name in ("Дом", "Школа")
    ]
    member = team.add(first.id, employee.id, shift_rate="2000", actor=1001)
    team.add(second.id, employee.id, shift_rate="3500", actor=1001)
    args = {
        "employee_ids": [employee.id],
        "work_date": date(2026, 1, 2),
        "coefficient": "1",
        "actor": 1001,
        "require_assignment": True,
    }
    one = services["attendance"].create_bulk(**args, object_id=first.id, operation_key="first")[0]
    two = services["attendance"].create_bulk(**args, object_id=second.id, operation_key="second")[0]
    assert one.rate_snapshot == Decimal(2000)
    assert two.rate_snapshot == Decimal(3500)
    assert team.roster(first.id)[0][2] == team.roster(second.id)[0][2] == 1
    assert (
        services["attendance"].create_bulk(**args, object_id=first.id, operation_key="first")[0].id
        == one.id
    )
    with pytest.raises(Conflict):
        services["attendance"].create_bulk(**args, object_id=first.id, operation_key="duplicate")
    with pytest.raises(Conflict):
        team.add(first.id, employee.id, shift_rate=None, actor=1001)
    team.set_rate(member.id, "2500", actor=1001)
    three = services["attendance"].create_bulk(
        **(args | {"work_date": date(2026, 1, 3)}), object_id=first.id, operation_key="third"
    )[0]
    assert three.rate_snapshot == Decimal(2500)
    assert services["attendance"].get(one.id).rate_snapshot == Decimal(2000)
    assert team.roster(first.id)[0][2] == 2
    assert team.roster(second.id)[0][2] == 1
    services["attendance"].void(one.id, actor=1001, reason="Ошибка")
    assert team.roster(first.id)[0][2] == 1
    assert team.roster(second.id)[0][2] == 1
    assert team.employee_objects(employee.id)[0][1] == 1


def test_roster_required_and_rate_optional(services):
    team = TeamService(services["sessions"])
    obj = services["objects"].create(name="Дом", start_date=date(2026, 1, 1), actor=1001)
    employee = services["employees"].create(
        name="Пётр", currency="RUB", start_date=date(2026, 1, 1), actor=1001
    )
    args = {
        "employee_ids": [employee.id],
        "object_id": obj.id,
        "work_date": date(2026, 1, 2),
        "coefficient": "1",
        "require_assignment": True,
    }
    with pytest.raises(DomainError, match="состав объекта"):
        services["attendance"].preview(**args)
    with pytest.raises(DomainError, match="состав объекта"):
        services["attendance"].create_bulk(**args, actor=1001, operation_key="not-assigned")
    member = team.add(obj.id, employee.id, shift_rate=None, actor=1001)
    with pytest.raises(DomainError):
        team.set_rate(member.id, "NaN", actor=1001)
    assert services["attendance"].preview(**args)[0].rate is None
    row = services["attendance"].create_bulk(**args, actor=1001, operation_key="without-rate")[0]
    assert row.rate_snapshot is row.earned_amount is None
    assert team.roster(obj.id)[0][2] == 1
    team.set_rate(member.id, "3000", actor=1001)
    assert services["attendance"].get(row.id).rate_snapshot is None
    assert team.available(obj.id) == []


def test_full_object_workflow_and_creation_during_shift(tmp_path):
    url = f"sqlite:///{tmp_path / 'workflow.db'}"
    sessions = make_session_factory(url)
    create_schema(sessions)
    configured_bot, dispatcher = compose(
        Settings(
            bot_token="123456:abcdefghijklmnopqrstuvwxyzABCDE",
            owner_ids={1001},
            database_url=url,
            timezone="UTC",
        )
    )
    bot = FakeBot()
    team = TeamService(sessions)
    employees = EmployeeService(sessions)
    attendance = AttendanceService(sessions)

    async def scenario():
        counter = 10000

        async def send(text, user=1001):
            nonlocal counter
            counter += 1
            await dispatcher.feed_update(bot, telegram_message(counter, user, text))

        async def click(value, user=1001):
            nonlocal counter
            counter += 1
            event = telegram_callback(counter, user, value)
            markup = next(
                (
                    call.reply_markup
                    for call in reversed(bot.calls)
                    if getattr(call, "reply_markup", None)
                    and hasattr(call.reply_markup, "inline_keyboard")
                ),
                None,
            )
            event = event.model_copy(
                update={
                    "callback_query": event.callback_query.model_copy(
                        update={
                            "message": event.callback_query.message.model_copy(
                                update={"reply_markup": markup}
                            )
                        }
                    )
                }
            )
            await dispatcher.feed_update(bot, event)

        async def create_object(name):
            await send("🏗 Объекты")
            await click("obj:create")
            await send(name)
            await send("Адрес")
            await click("objectdate:today")
            with sessions() as session:
                return session.scalar(select(WorkObject).where(WorkObject.name == name)).id

        first = await create_object("Первый объект")
        await click("team:add")
        await click("emp:create")
        await send("Иванов Иван Иванович")
        await send("+79990000000")
        await send("Тестовый банк, СБП +79990000000")
        await send("-")
        await send("2000")
        assert "Проверьте карточку" in bot.calls[-1].text
        with sessions() as session:
            assert session.scalar(select(func.count()).select_from(Employee)) == 0
        await click("person:save")
        with sessions() as session:
            employee = session.scalar(select(Employee))
            assert employee.phone == "+79990000000"
            assert employee.payment_details.startswith("Тестовый банк")
        assert team.roster(first)[0][2] == 0
        second = await create_object("Второй объект")
        await click("team:add")
        await click("team:existing")
        await click(f"attach:{employee.id}")
        await send("3500")
        await click("person:save")
        with sessions() as session:
            assert session.scalar(select(func.count()).select_from(Employee)) == 1
            assert session.scalar(select(func.count()).select_from(ObjectEmployee)) == 2

        await click(f"shift:{first}")
        await click("shiftdate:today")
        await click(f"toggle:{employee.id}")
        await click("team:add")
        await click("emp:create")
        for text in ["Петров Пётр Петрович", "-", "-", "-", "-"]:
            await send(text)
        await click("person:save")
        assert "Отметьте пришедших" in bot.calls[-1].text
        assert (
            sum(
                button.text.startswith("☑")
                for row in bot.calls[-1].reply_markup.inline_keyboard
                for button in row
            )
            == 2
        )
        await click("shift:preview")
        assert "Иванов" in bot.calls[-1].text and "Петров" in bot.calls[-1].text
        await click("shift:save")
        assert [count for _, _, count in team.roster(first)] == [1, 1]
        assert team.roster(second)[0][2] == 0
        await click("shift:save")  # stale confirmation cannot add more shifts
        assert [count for _, _, count in team.roster(first)] == [1, 1]

        await click(f"shift:{second}")
        await click("shiftdate:today")
        assert (
            len(
                [
                    b
                    for row in bot.calls[-2].reply_markup.inline_keyboard
                    for b in row
                    if b.callback_data.startswith("toggle:")
                ]
            )
            == 1
        )
        await click(f"toggle:{employee.id}")
        await click("shift:preview")
        await click("shift:save")
        assert team.roster(second)[0][2] == 1
        day = datetime.now(UTC).date()
        assert next(
            row for row in team.day(first, day) if row.employee_id == employee.id
        ).rate_snapshot == Decimal(2000)
        assert team.day(second, day)[0].rate_snapshot == Decimal(3500)
        await click(f"shift:{second}")
        await click("shiftdate:today")
        assert any(
            b.callback_data == "shifted"
            for row in bot.calls[-2].reply_markup.inline_keyboard
            for b in row
        )

        await click(f"obj:{first}")
        await click("team:list")
        member = next(member for member, emp, _ in team.roster(first) if emp.id == employee.id)
        await click(f"member:{member.id}")
        await click("member:history")
        assert "История смен" in bot.calls[-1].text
        row = next(row for row in team.day(first, day) if row.employee_id == employee.id)
        await click(f"undo:{row.id}")
        assert attendance.get(row.id).voided_at is None
        await click("undo:confirm")
        assert attendance.get(row.id).voided_at is not None
        assert next(count for _, emp, count in team.roster(first) if emp.id == employee.id) == 0
        assert team.roster(second)[0][2] == 1
        await click("teamcsv")
        assert b"\xd0\xa1\xd0\xbc\xd0\xb5\xd0\xbd\xd1\x8b" in bot.calls[-1].document.data

        await click(f"empedit:{employee.id}")
        await click("personfield:payment_details")
        await send("-")
        assert employees.get(employee.id).payment_details is None
        await click(f"member:{member.id}")
        await click("member:rate")
        await send("2500")
        assert team.get(member.id).shift_rate == Decimal(2500)
        await click(f"shift:{first}", user=9999)
        assert "недоступно" in bot.calls[-1].text
        with sessions() as session:
            assert session.scalar(select(func.count()).select_from(Attendance)) == 3

        await click(f"obj:{first}")
        await click("object:edit")
        await click("objectfield:description")
        await send("Описание объекта")
        await click("objectfield:comment")
        await send("Примечание объекта")
        await click("objectfield:start_date")
        await send("01.01.2020")
        with sessions() as session:
            obj = session.get(WorkObject, first)
            assert obj.description == "Описание объекта"
            assert obj.comment == "Примечание объекта"
            assert obj.start_date == date(2020, 1, 1)
        await click("objectfield:start_date")
        await send("01.01.2099")
        assert "позже существующих смен" in bot.calls[-1].text
        await send("/cancel")
        await click(f"empedit:{employee.id}")
        await click("personfield:comment")
        await send("Примечание сотрудника")
        await click("personfield:start_date")
        await send("01.01.2020")
        assert employees.get(employee.id).comment == "Примечание сотрудника"
        assert employees.get(employee.id).start_date == date(2020, 1, 1)
        await click("personfield:start_date")
        await send("01.01.2099")
        assert "позже существующих смен" in bot.calls[-1].text
        await send("/cancel")

        await click(f"delete:emp:{employee.id}")
        assert employees.get(employee.id).status == "active"
        await click("delete:cancel")
        assert employees.get(employee.id).status == "active"
        await click(f"delete:emp:{employee.id}")
        await click(f"deleteok:obj:{first}")  # wrong confirmation must not delete another card
        with sessions() as session:
            assert session.get(WorkObject, first).status == "active"
        await click(f"deleteok:emp:{employee.id}")
        assert employees.get(employee.id).status == "inactive"
        assert not any(
            b.callback_data == f"emp:{employee.id}"
            for row in bot.calls[-1].reply_markup.inline_keyboard
            for b in row
        )
        assert team.roster(second)[0][2] == 1
        assert employee.id not in {row.id for row in team.available(second)}
        await click("employees:deleted")
        assert any(
            b.callback_data == f"emp:{employee.id}"
            for row in bot.calls[-1].reply_markup.inline_keyboard
            for b in row
        )
        await click(f"restore:emp:{employee.id}")
        assert employees.get(employee.id).status == "active"

        await click(f"delete:obj:{first}")
        await click(f"deleteok:obj:{first}")
        with sessions() as session:
            assert session.get(WorkObject, first).status == "archived"
        assert not any(
            b.callback_data == f"obj:{first}"
            for row in bot.calls[-1].reply_markup.inline_keyboard
            for b in row
        )
        await click(f"shift:{first}")
        assert "закрыт" in bot.calls[-1].text
        await click("objects:deleted")
        assert any(
            b.callback_data == f"obj:{first}"
            for row in bot.calls[-1].reply_markup.inline_keyboard
            for b in row
        )
        await click(f"restore:obj:{first}")
        await click(f"delete:obj:{first}", user=9999)
        await click(f"deleteok:obj:{first}", user=9999)
        with sessions() as session:
            assert session.get(WorkObject, first).status == "active"
            assert session.scalar(select(func.count()).select_from(Attendance)) == 3
            assert session.scalar(select(func.count()).select_from(ObjectEmployee)) == 3
        await bot.session.close()
        await configured_bot.session.close()

    asyncio.run(scenario())
