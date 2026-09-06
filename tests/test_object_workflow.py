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
        name="Иван", start_date=date(2026, 1, 1), actor=1001
    )
    first, second = [
        services["objects"].create(name=name, start_date=date(2026, 1, 1), actor=1001)
        for name in ("Дом", "Школа")
    ]
    member = team.add_many(first.id, [employee.id], actor=1001)[0]
    second_member = team.add_many(second.id, [employee.id], actor=1001)[0]
    team.set_rate(member.id, "2000", actor=1001)
    team.set_rate(second_member.id, "3500", actor=1001)
    args = {
        "employee_ids": [employee.id],
        "work_date": date(2026, 1, 2),
        "actor": 1001,
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
        team.add_many(first.id, [employee.id], actor=1001)
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
        name="Пётр", start_date=date(2026, 1, 1), actor=1001
    )
    args = {
        "employee_ids": [employee.id],
        "object_id": obj.id,
        "work_date": date(2026, 1, 2),
    }
    with pytest.raises(DomainError, match="состав объекта"):
        services["attendance"].preview(**args)
    with pytest.raises(DomainError, match="состав объекта"):
        services["attendance"].create_bulk(**args, actor=1001, operation_key="not-assigned")
    member = team.add_many(obj.id, [employee.id], actor=1001)[0]
    with pytest.raises(DomainError):
        team.set_rate(member.id, "NaN", actor=1001)
    assert services["attendance"].preview(**args)[0].id == employee.id
    row = services["attendance"].create_bulk(**args, actor=1001, operation_key="without-rate")[0]
    assert row.rate_snapshot is row.earned_amount is None
    assert team.roster(obj.id)[0][2] == 1
    team.set_rate(member.id, "3000", actor=1001)
    assert services["attendance"].get(row.id).rate_snapshot is None
    assert team.available(obj.id) == []


def test_mass_attach_and_effective_object_rate_keep_attendance_snapshots(services):
    team = TeamService(services["sessions"])
    obj = services["objects"].create(name="Дом", start_date=date(2026, 1, 1), actor=1001)
    first = services["employees"].create(
        name="Иван",
        start_date=date(2026, 1, 1),
        actor=1001,
    )
    second = services["employees"].create(
        name="Руслан", start_date=date(2026, 1, 1), actor=1001
    )
    assert {row.id for row in team.available(obj.id)} == {first.id, second.id}
    members = team.add_many(obj.id, [first.id, second.id], actor=1001)
    assert len(members) == 2
    team.set_rate(members[0].id, "2000", actor=1001)
    assert team.get(members[0].id).shift_rate == Decimal(2000)
    assert team.get(members[1].id).shift_rate is None
    assert team.available(obj.id) == []
    with pytest.raises(Conflict):
        team.add_many(obj.id, [first.id], actor=1001)

    old = services["attendance"].create_bulk(
        employee_ids=[first.id],
        object_id=obj.id,
        work_date=date(2026, 1, 2),
        actor=1001,
        operation_key="base-rate",
    )[0]
    team.set_rate(members[0].id, "2500", actor=1001)
    new = services["attendance"].create_bulk(
        employee_ids=[first.id],
        object_id=obj.id,
        work_date=date(2026, 1, 3),
        actor=1001,
        operation_key="object-rate",
    )[0]
    assert old.rate_snapshot == Decimal(2000)
    assert new.rate_snapshot == Decimal(2500)
    assert services["payroll"].summary(first.id, object_id=obj.id).earned == Decimal(4500)


def test_membership_removal_preserves_separate_balances_and_history(services):
    team = TeamService(services["sessions"])
    employee = services["employees"].create(
        name="Иван", start_date=date(2026, 1, 1), actor=1001
    )
    first, second = [
        services["objects"].create(name=name, start_date=date(2026, 1, 1), actor=1001)
        for name in ("Дом", "Школа")
    ]
    member = team.add_many(first.id, [employee.id], actor=1001)[0]
    second_member = team.add_many(second.id, [employee.id], actor=1001)[0]
    team.set_rate(member.id, "2000", actor=1001)
    team.set_rate(second_member.id, "3500", actor=1001)
    args = {
        "employee_ids": [employee.id],
        "work_date": date(2026, 1, 2),
        "actor": 1001,
    }
    first_shift = services["attendance"].create_bulk(
        **args, object_id=first.id, operation_key="one"
    )[0]
    services["attendance"].create_bulk(**args, object_id=second.id, operation_key="two")
    payment_args = {
        "employee_id": employee.id,
        "payment_date": date(2026, 1, 2),
        "actor": 1001,
    }
    payment = services["payments"].create(
        **payment_args, object_id=first.id, amount="500", idempotency_key="paid-one"
    )
    services["payments"].create(
        **payment_args, object_id=second.id, amount="4000", idempotency_key="advance-two"
    )
    team.set_active(member.id, False, actor=1001)
    assert team.roster(first.id, active_only=True) == []
    assert team.roster(first.id)[0][2] == 1
    assert team.roster(second.id, active_only=True)[0][2] == 1
    assert services["employees"].get(employee.id).status == "active"
    assert employee.id in {row.id for row in team.available(first.id)}
    summary = services["payroll"].summary(employee.id, object_id=first.id)
    assert (summary.earned, summary.paid, summary.balance) == (2000, 500, 1500)
    assert services["payroll"].summary(employee.id, object_id=second.id).balance == -500
    assert team.history(first.id, employee.id)[0].id == first_shift.id
    assert services["payments"].history(employee.id, first.id)[0].id == payment.id
    with pytest.raises(DomainError, match="убран"):
        services["attendance"].create_bulk(
            **(args | {"work_date": date(2026, 1, 3)}),
            object_id=first.id,
            operation_key="removed",
        )
    # Debts can be settled after removal from the team.
    services["payments"].create(
        **payment_args, object_id=first.id, amount="1500", idempotency_key="settlement"
    )
    assert services["payroll"].summary(employee.id, object_id=first.id).balance == 0
    team.set_active(member.id, True, actor=1001)
    assert team.get(member.id).shift_rate == 2000
    team.set_active(member.id, False, actor=1001)
    returned = team.add_many(first.id, [employee.id], actor=1001)[0]
    assert returned.id == member.id
    team.set_rate(returned.id, "2500", actor=1001)
    services["attendance"].create_bulk(
        **(args | {"work_date": date(2026, 1, 3)}), object_id=first.id, operation_key="returned"
    )
    assert team.roster(first.id)[0][2] == 2
    assert services["attendance"].get(first_shift.id).rate_snapshot == 2000
    assert services["payroll"].summary(employee.id, object_id=first.id).balance == 2500
    services["payments"].void(payment.id, actor=1001, reason="Ошибка")
    assert services["payroll"].summary(employee.id, object_id=first.id).balance == 3000
    assert services["payroll"].summary(employee.id, object_id=second.id).balance == -500
    team.set_rate(member.id, None, actor=1001)
    services["attendance"].create_bulk(
        **(args | {"work_date": date(2026, 1, 4)}), object_id=first.id, operation_key="unrated"
    )
    assert services["payroll"].summary(employee.id, object_id=first.id).unrated_shifts == 1


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

        def action(prefix):
            markup = next(
                call.reply_markup
                for call in reversed(bot.calls)
                if getattr(call, "reply_markup", None)
                and hasattr(call.reply_markup, "inline_keyboard")
            )
            return next(
                button.callback_data
                for row in markup.inline_keyboard
                for button in row
                if button.callback_data.startswith(prefix)
            )

        async def create_object(name):
            await send("🏗 Объекты")
            await click("obj:create")
            await send(name)
            await send("Адрес")
            await click("objectdate:today")
            with sessions() as session:
                return session.scalar(select(WorkObject).where(WorkObject.name == name)).id

        await send("/start")
        assert "Выберите объект" in bot.calls[-1].text
        main_keyboard = bot.calls[-2].reply_markup.keyboard
        assert [button.text for row in main_keyboard for button in row] == [
            "🏗 Объекты",
            "👷 База сотрудников",
        ]
        first = await create_object("Первый объект")
        object_screen = next(
            call for call in reversed(bot.calls) if getattr(call, "reply_markup", None)
        )
        assert "Рабочие на объекте: 0" in object_screen.text
        assert any(
            button.callback_data == f"teamadd:{first}"
            for row in object_screen.reply_markup.inline_keyboard
            for button in row
        )
        await click(f"teamadd:{first}")
        await click("emp:create")
        await send("Иванов Иван Иванович")
        await send("Тестовый банк, СБП +79990000000")
        await send("2000")
        assert "Проверьте карточку" in bot.calls[-1].text
        with sessions() as session:
            assert session.scalar(select(func.count()).select_from(Employee)) == 0
        await click(action("personsave:"))
        with sessions() as session:
            employee = session.scalar(select(Employee))
            assert employee.phone is None
            assert employee.payment_details.startswith("Тестовый банк")
        assert team.roster(first)[0][2] == 0
        second = await create_object("Второй объект")
        await click(f"teamadd:{second}")
        await click(action(f"addtoggle:{employee.id}:"))
        assert "Добавить выбранных: 1" in next(
            button.text
            for row in bot.calls[-1].reply_markup.inline_keyboard
            for button in row
            if button.callback_data.startswith("addsave:")
        )
        mass_add_save = action("addsave:")
        await click(mass_add_save)
        await click(mass_add_save)
        second_member = team.roster(second)[0][0]
        await click(f"memberrate:{second_member.id}")
        await send("3500")
        with sessions() as session:
            assert session.scalar(select(func.count()).select_from(Employee)) == 1
            assert session.scalar(select(func.count()).select_from(ObjectEmployee)) == 2

        await click(f"shift:{first}")
        await click(action(f"toggle:{employee.id}:"))
        await click(action("teamadd:"))
        await click("emp:create")
        await send("Петров Пётр Петрович")
        await click("person:skipdetails")
        await click("person:skiprate")
        await click(action("personsave:"))
        assert "Отметьте пришедших" in bot.calls[-1].text
        assert (
            sum(
                button.text.startswith("☑")
                for row in bot.calls[-1].reply_markup.inline_keyboard
                for button in row
            )
            == 2
        )
        await click(action("shift:preview:"))
        assert "Иванов" in bot.calls[-1].text and "Петров" in bot.calls[-1].text
        saved_confirmation = action("shiftsave:")
        await click(saved_confirmation)
        assert [count for _, _, count in team.roster(first)] == [1, 1]
        assert team.roster(second)[0][2] == 0
        await click(saved_confirmation)  # stale confirmation cannot add more shifts
        assert [count for _, _, count in team.roster(first)] == [1, 1]

        await click(f"shift:{second}")
        assert (
            len(
                [
                    b
                    for row in bot.calls[-1].reply_markup.inline_keyboard
                    for b in row
                    if b.callback_data.startswith("toggle:")
                ]
            )
            == 1
        )
        await click(action(f"toggle:{employee.id}:"))
        await click(action("shift:preview:"))
        saved_confirmation = action("shiftsave:")
        await click(saved_confirmation)
        assert team.roster(second)[0][2] == 1
        day = datetime.now(UTC).date()
        assert next(
            row for row in team.day(first, day) if row.employee_id == employee.id
        ).rate_snapshot == Decimal(2000)
        assert team.day(second, day)[0].rate_snapshot == Decimal(3500)
        await click(f"shift:{second}")
        assert any(
            b.callback_data == "shifted"
            for row in bot.calls[-1].reply_markup.inline_keyboard
            for b in row
        )

        await click(f"obj:{first}")
        assert "Иванов Иван Иванович — 2 000 ₽/смена • 1 смен" in bot.calls[-1].text
        assert "Петров Пётр Петрович — не указана ₽/смена • 1 смен" in bot.calls[-1].text
        member = next(member for member, emp, _ in team.roster(first) if emp.id == employee.id)
        await click(f"member:{member.id}")
        assert "Ставка: 2 000 ₽/смена" in bot.calls[-1].text
        assert any(
            button.callback_data == f"quickshift:{member.id}"
            for row in bot.calls[-1].reply_markup.inline_keyboard
            for button in row
        )
        await click(f"memberlog:{member.id}")
        await click(f"shifts:{member.id}:0")
        assert "История смен" in bot.calls[-1].text
        row = next(row for row in team.day(first, day) if row.employee_id == employee.id)
        await click(f"undo:{row.id}")
        assert attendance.get(row.id).voided_at is None
        await click(f"undook:{row.id}")
        assert attendance.get(row.id).voided_at is not None
        assert next(count for _, emp, count in team.roster(first) if emp.id == employee.id) == 0
        assert team.roster(second)[0][2] == 1
        await click(f"quickshift:{member.id}")
        assert "Иванов Иван Иванович — +1 смена" in bot.calls[-1].text
        await click("cancel")
        await click(f"teamcsv:{first}")
        assert b"\xd0\xa1\xd0\xbc\xd0\xb5\xd0\xbd\xd1\x8b" in bot.calls[-1].document.data

        await click(f"empedit:{employee.id}")
        await click(f"personfield:payment_details:{employee.id}")
        await send("-")
        assert employees.get(employee.id).payment_details is None
        await click(f"member:{member.id}")
        await click(f"memberrate:{member.id}")
        await send("2500")
        assert team.get(member.id).shift_rate == Decimal(2500)
        await click(f"shift:{first}", user=9999)
        assert "недоступно" in bot.calls[-1].text
        with sessions() as session:
            assert session.scalar(select(func.count()).select_from(Attendance)) == 3

        await click(f"obj:{first}")
        await click(f"objsettings:{first}")
        await click(f"objectfield:description:{first}")
        await send("Описание объекта")
        await click(f"objectfield:comment:{first}")
        await send("Примечание объекта")
        await click(f"objectfield:start_date:{first}")
        await send("01.01.2020")
        with sessions() as session:
            obj = session.get(WorkObject, first)
            assert obj.description == "Описание объекта"
            assert obj.comment == "Примечание объекта"
            assert obj.start_date == date(2020, 1, 1)
        await click(f"objectfield:start_date:{first}")
        await send("01.01.2099")
        assert "позже существующих смен" in bot.calls[-1].text
        await send("/cancel")
        await click(f"empedit:{employee.id}")
        await click(f"personfield:comment:{employee.id}")
        await send("Примечание сотрудника")
        await click(f"personfield:start_date:{employee.id}")
        await send("01.01.2020")
        assert employees.get(employee.id).comment == "Примечание сотрудника"
        assert employees.get(employee.id).start_date == date(2020, 1, 1)
        await click(f"personfield:start_date:{employee.id}")
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

        await click(f"member:{member.id}")
        assert "Выплачено: 0.00 ₽" in bot.calls[-1].text
        await click(f"pay:{member.id}")
        await send("NaN")
        await send("3000")
        assert "Записать выплату?" in bot.calls[-1].text
        original_payment_confirm = action("payok:")
        await click("payment:date")
        await click("paydate:custom")
        await send("01.01.2099")
        assert "будущей" in bot.calls[-1].text
        await send(datetime.now(UTC).strftime("%d.%m.%Y"))
        await click("payment:comment")
        await send("Аванс")
        payment_confirm = action("payok:")
        await click(original_payment_confirm)
        await click(payment_confirm)
        assert "Аванс: 3000.00 ₽" in bot.calls[-1].text
        await click(payment_confirm)  # repeating the confirmation cannot create a second payment
        await click(f"pays:{member.id}:0")
        payment_void = action("payvoid:")
        assert len(bot.calls[-1].reply_markup.inline_keyboard) == 2
        await click(f"membership:0:{member.id}")
        remove_confirm = action("membershipok:")
        assert team.get(member.id).active
        await click(remove_confirm)
        assert not team.get(member.id).active
        assert "Аванс: 3000.00 ₽" in bot.calls[-1].text
        assert employees.get(employee.id).status == "active"
        await click(f"obj:{first}")
        assert not any(
            button.callback_data == f"member:{member.id}"
            for row in bot.calls[-1].reply_markup.inline_keyboard
            for button in row
        )
        await click(f"teamformer:{first}")
        assert any(
            button.callback_data == f"member:{member.id}"
            for row in bot.calls[-1].reply_markup.inline_keyboard
            for button in row
        )
        await click(f"shift:{first}")
        picker = next(call for call in reversed(bot.calls) if getattr(call, "reply_markup", None))
        assert not any(
            button.callback_data.startswith(f"toggle:{employee.id}:")
            for row in picker.reply_markup.inline_keyboard
            for button in row
        )
        await click(f"membership:1:{member.id}")
        restore_confirm = action("membershipok:")
        await click(remove_confirm)  # an old confirmation cannot approve the new operation
        assert not team.get(member.id).active
        await click(restore_confirm)
        assert team.get(member.id).active
        assert team.get(member.id).shift_rate == 2500
        other_member = team.roster(second)[0][0]
        await click(f"pay:{other_member.id}")
        await send("500")
        other_confirm = action("payok:")
        await click(payment_confirm)  # stale first-object approval cannot pay on the second object
        await click(other_confirm)
        assert "Осталось выплатить: 3000.00 ₽" in bot.calls[-1].text
        await click(payment_void)
        void_confirm = action("payvoidok:")
        await click(void_confirm, user=9999)
        await click(void_confirm)
        assert "Выплачено: 0.00 ₽" in bot.calls[-1].text
        await click(f"member:{other_member.id}")
        assert "Выплачено: 500.00 ₽" in bot.calls[-1].text
        await click(f"member:{member.id}")
        await click(f"teamcsv:{first}")
        assert "Выплачено" in bot.calls[-1].document.data.decode("utf-8-sig")
        await click(f"shift:{first}")
        await click(action(f"toggle:{employee.id}:"))
        await click(action("shift:date:"))
        await click("shiftdate:yesterday")
        picker = next(call for call in reversed(bot.calls) if getattr(call, "reply_markup", None))
        assert not any(
            button.text.startswith("☑")
            for row in picker.reply_markup.inline_keyboard
            for button in row
        )
        await click(action("shift:date:"))
        await click("shiftdate:custom")
        await send(datetime.now(UTC).strftime("%d.%m.%Y"))
        assert "Отметьте пришедших" in bot.calls[-1].text
        await click(action("teamadd:"))
        await click("add:back")
        assert "Отметьте пришедших" in bot.calls[-1].text
        await click(f"pay:{member.id}")
        await send("/cancel")
        assert "Выплачено: 0.00 ₽" in bot.calls[-1].text
        # A bound object button must open its original object even after another card was opened.
        await click(f"obj:{second}")
        assert "Второй объект" in bot.calls[-1].text
        await click(f"reportobj:{first}")
        assert "Первый объект" in bot.calls[-1].text
        await click(f"teamxlsx:{first}")
        assert bot.calls[-1].document.filename == f"shifts-{first}.xlsx"
        await click(f"membership:0:{member.id}", user=9999)
        await click(remove_confirm, user=9999)
        assert team.get(member.id).active
        await bot.session.close()
        await configured_bot.session.close()

    asyncio.run(scenario())
