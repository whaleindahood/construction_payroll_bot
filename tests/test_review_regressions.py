import asyncio
from datetime import UTC, date, datetime

import pytest

from app.config import Settings
from app.main import compose
from app.services import Conflict
from app.teams import TeamService
from tests.test_bot_end_to_end import FakeBot, telegram_message
from tests.test_employee_self_service import telegram_callback


def test_pricing_and_payments_keep_all_worked_shifts(services):
    employee = services["employees"].create(
        name="Иван", start_date=date(2020, 1, 1), actor=1001
    )
    objects = [
        services["objects"].create(name=name, start_date=date(2020, 1, 1), actor=1001)
        for name in ("Дом", "Школа")
    ]
    team = TeamService(services["sessions"])
    members = [team.add_many(obj.id, [employee.id], actor=1001)[0] for obj in objects]
    rows = [
        services["attendance"].create_bulk(
            employee_ids=[employee.id],
            object_id=obj.id,
            work_date=date(2020, 1, 2),
            actor=1001,
            operation_key=obj.id,
        )[0]
        for obj in objects
    ]
    services["attendance"].price_unrated(rows[0].id, "2000", actor=1001)
    with pytest.raises(Conflict):
        services["attendance"].price_unrated(rows[0].id, "3000", actor=1001)
    services["payments"].create(
        employee_id=employee.id,
        object_id=objects[0].id,
        payment_date=date(2020, 1, 2),
        amount="2000",
        actor=1001,
        idempotency_key="full-payment",
    )
    assert services["payroll"].summary(employee.id, object_id=objects[0].id).balance == 0
    assert team.roster(objects[0].id)[0][2] == 1
    assert team.history(objects[0].id, employee.id)[0].id == rows[0].id
    assert team.unrated(members[0].id) == []
    assert len(team.unrated(members[1].id)) == 1
    assert services["attendance"].get(rows[0].id).earned_amount == 2000
    assert team.get(members[0].id).shift_rate is None
    services["attendance"].void(rows[1].id, actor=1001, reason="Ошибка")
    with pytest.raises(Conflict):
        services["attendance"].price_unrated(rows[1].id, "500", actor=1001)


def test_bound_buttons_never_target_current_unrelated_card(services):
    sessions = services["sessions"]
    employee = services["employees"].create(
        name="Иван", start_date=date(2020, 1, 1), actor=1001
    )
    one, two = [
        services["objects"].create(name=name, start_date=date(2020, 1, 1), actor=1001)
        for name in ("Дом", "Школа")
    ]
    team = TeamService(sessions)
    a, b = [team.add_many(obj.id, [employee.id], actor=1001)[0] for obj in (one, two)]
    team.set_rate(a.id, "2000", actor=1001)
    team.set_rate(b.id, "2000", actor=1001)
    configured, dispatcher = compose(
        Settings(
            bot_token="123456:abcdefghijklmnopqrstuvwxyzABCDE",
            owner_ids={1001},
            database_url=str(sessions.kw["bind"].url),
            timezone="UTC",
        )
    )
    bot = FakeBot()

    async def scenario():
        counter = 50000

        def markup():
            return next(
                call.reply_markup
                for call in reversed(bot.calls)
                if getattr(call, "reply_markup", None)
                and hasattr(call.reply_markup, "inline_keyboard")
            )

        def action(prefix):
            return next(
                button.callback_data
                for row in markup().inline_keyboard
                for button in row
                if button.callback_data.startswith(prefix)
            )

        async def click(value, source=None, user=1001):
            nonlocal counter
            counter += 1
            event = telegram_callback(counter, user, value)
            if bot.calls:
                keyboard = source or markup()
                assert all(
                    len(button.callback_data.encode("utf-8")) <= 64
                    for row in keyboard.inline_keyboard
                    for button in row
                )
                event = event.model_copy(
                    update={
                        "callback_query": event.callback_query.model_copy(
                            update={
                                "message": event.callback_query.message.model_copy(
                                    update={"reply_markup": keyboard}
                                )
                            }
                        )
                    }
                )
            await dispatcher.feed_update(bot, event)

        async def send(value):
            nonlocal counter
            counter += 1
            await dispatcher.feed_update(bot, telegram_message(counter, 1001, value))

        await click(f"shift:{one.id}")
        old_toggle, old_picker = action("toggle:"), markup()
        await click(old_toggle)
        await click(action("shift:preview:"))
        old_save, old_confirm = action("shiftsave:"), markup()
        await click(f"shift:{two.id}")
        await click(old_toggle, old_picker)
        await click(action("shift:preview:"))
        assert "Выберите хотя бы" in bot.calls[-1].text
        await click(action("toggle:"))
        await click(action("shift:preview:"))
        new_save = action("shiftsave:")
        await click(old_save, old_confirm)
        today = datetime.now(UTC).date()
        assert team.day(one.id, today) == team.day(two.id, today) == []
        await click(new_save)
        assert len(team.day(two.id, today)) == 1
        await click(new_save)
        assert len(team.day(two.id, today)) == 1

        await click(f"member:{a.id}")
        old_rate, old_settings = action("memberrate:"), markup()
        await click(f"member:{b.id}")
        await click(old_rate, old_settings)
        await send("999")
        assert team.get(a.id).shift_rate == 999
        assert team.get(b.id).shift_rate == 2000

        await click(f"objsettings:{one.id}")
        old_field, old_settings = action("objectfield:comment:"), markup()
        await click(f"objsettings:{two.id}")
        await click(old_field, old_settings)
        await send("Заметка для дома")
        assert services["objects"].get(one.id).comment == "Заметка для дома"
        assert services["objects"].get(two.id).comment is None

        team.set_rate(a.id, None, actor=1001)
        row_a = services["attendance"].create_bulk(
            employee_ids=[employee.id],
            object_id=one.id,
            work_date=today,
            actor=1001,
            operation_key="unrated",
        )[0]
        row_b = team.day(two.id, today)[0]
        await click(f"undo:{row_a.id}")
        old_undo, old_confirm = action("undook:"), markup()
        await click(f"undo:{row_b.id}")
        await click(old_undo, old_confirm)
        assert services["attendance"].get(row_a.id).voided_at is None
        assert services["attendance"].get(row_b.id).voided_at is None
        await click(f"undook:{row_b.id}")
        assert services["attendance"].get(row_b.id).voided_at is not None

        await click(f"member:{a.id}")
        await click(action("unrated:"))
        await click(action("price:"))
        await send("1500")
        confirmation = action("priceok:")
        await click(confirmation, user=9999)
        assert services["attendance"].get(row_a.id).earned_amount is None
        await click(confirmation)
        assert services["attendance"].get(row_a.id).earned_amount == 1500
        await click(confirmation)
        assert team.roster(one.id)[0][2] == 1
        await click(f"pay:{a.id}")
        await send("1500")
        await click(action("payok:"))
        assert "Смен: 1" in bot.calls[-1].text
        assert "Осталось выплатить: 0.00 ₽" in bot.calls[-1].text
        await click(f"shifts:{a.id}:0")
        assert action("undo:") == f"undo:{row_a.id}"
        await bot.session.close()
        await configured.session.close()

    asyncio.run(scenario())
