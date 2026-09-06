from __future__ import annotations

import asyncio
from datetime import UTC, date, datetime, timedelta

import pytest
from aiogram.types import CallbackQuery, Chat, Message, Update
from aiogram.types import User as TelegramUser
from sqlalchemy import func, select

from app.config import Settings
from app.db import create_schema, make_session_factory
from app.main import compose
from app.models import AuditLog, Employee, User
from app.services import Conflict, DomainError, EmployeeService
from tests.test_bot_end_to_end import FakeBot, telegram_message


def create_employee(employees, name="Иван", telegram_id=None):
    return employees.create(
        name=name,
        start_date=date(2026, 1, 1),
        actor=1001,
        telegram_id=telegram_id,
    )


def test_invites_rotate_expire_and_bind_only_once(services):
    employees = services["employees"]
    first = create_employee(employees)
    second = create_employee(employees, "Пётр")
    old = employees.create_invite(first.id, actor=1001)
    token = employees.create_invite(first.id, actor=1001)
    with pytest.raises(Conflict):
        employees.accept_invite(old, telegram_id=2001)
    assert employees.get(first.id).invite_token_hash != token
    assert employees.accept_invite(token, telegram_id=2001).id == first.id
    with pytest.raises(Conflict):
        employees.accept_invite(token, telegram_id=2002)
    with pytest.raises(Conflict):
        employees.create_invite(first.id, actor=1001)
    another = employees.create_invite(second.id, actor=1001)
    with pytest.raises(Conflict):
        employees.accept_invite(another, telegram_id=2001)
    with services["sessions"]() as session, session.begin():
        session.get(Employee, second.id).invite_expires_at = datetime.now(UTC) - timedelta(
            seconds=1
        )
    with pytest.raises(Conflict):
        employees.accept_invite(another, telegram_id=2002)
    disabled_token = employees.create_invite(second.id, actor=1001)
    employees.set_status(second.id, "inactive", actor=1001)
    employees.set_status(second.id, "active", actor=1001)
    with pytest.raises(Conflict):
        employees.accept_invite(disabled_token, telegram_id=2002)


def test_self_profile_is_scoped_validated_and_does_not_change_payroll(services):
    employees = services["employees"]
    first = create_employee(employees, telegram_id=2001)
    second = create_employee(employees, "Пётр", telegram_id=2002)
    updated = employees.update_own_profile(
        2001, name="Иванов Иван Иванович", payment_details="Тестовый банк, СБП +79990000000"
    )
    assert updated.id == first.id
    assert employees.get(second.id).name == "Пётр"
    assert employees.get(second.id).payment_details is None
    with pytest.raises(DomainError):
        employees.update_own_profile(2001, name="", payment_details="банк")
    with pytest.raises(DomainError):
        employees.update_own_profile(2001, name="Иван", payment_details="x" * 1001)
    with pytest.raises(DomainError):
        employees.update_own_profile(9999, name="Иван", payment_details="банк")
    employees.set_status(first.id, "inactive", actor=1001)
    assert employees.by_telegram(2001) is None
    with pytest.raises(DomainError):
        employees.update_own_profile(2001, name="Иван", payment_details="банк")
    with services["sessions"]() as session:
        assert (
            session.scalar(
                select(func.count())
                .select_from(AuditLog)
                .where(AuditLog.action == "employee_profile_updated")
            )
            == 1
        )


def telegram_callback(update_id, user_id, data, *, group=False):
    return Update(
        update_id=update_id,
        callback_query=CallbackQuery(
            id=str(update_id),
            chat_instance="test",
            from_user=TelegramUser(id=user_id, is_bot=False, first_name="Test"),
            data=data,
            message=Message(
                message_id=update_id,
                date=datetime.now(UTC),
                chat=Chat(id=-123 if group else user_id, type="group" if group else "private"),
            ),
        ),
    )


def test_employee_dialog_and_owner_access_boundaries(tmp_path):
    database_url = f"sqlite:///{tmp_path / 'bot.db'}"
    sessions = make_session_factory(database_url)
    create_schema(sessions)
    employees = EmployeeService(sessions)
    first = create_employee(employees)
    second = create_employee(employees, "Чужой сотрудник", telegram_id=2002)
    token = employees.create_invite(first.id, actor=1001)
    configured_bot, dispatcher = compose(
        Settings(
            bot_token="123456:abcdefghijklmnopqrstuvwxyzABCDE",
            owner_ids={1001},
            database_url=database_url,
        )
    )
    bot = FakeBot()
    bot._me = TelegramUser(id=123456, is_bot=True, first_name="Test", username="test_bot")

    async def scenario():
        counter = 100

        async def send(text, user=2001):
            nonlocal counter
            counter += 1
            await dispatcher.feed_update(bot, telegram_message(counter, user, text))

        async def click(data, user=2001, group=False):
            nonlocal counter
            counter += 1
            await dispatcher.feed_update(bot, telegram_callback(counter, user, data, group=group))

        await send(f"/start employee_{token}")
        assert "фамилию" in bot.calls[-1].text
        await send("Иванов Иван Иванович")
        await send("Тестовый банк, СБП +79990000000")
        assert "Проверьте" in bot.calls[-1].text
        assert employees.get(first.id).payment_details is None
        await click("profile:save")
        assert employees.get(first.id).name == "Иванов Иван Иванович"
        assert employees.get(first.id).payment_details == "Тестовый банк, СБП +79990000000"
        assert employees.get(second.id).payment_details is None
        await click("profile:save")  # repeated confirmation cannot write twice
        with sessions() as session:
            assert (
                session.scalar(
                    select(func.count())
                    .select_from(AuditLog)
                    .where(AuditLog.action == "employee_profile_updated")
                )
                == 1
            )
            assert session.get(User, 2001) is None  # employee never gains OWNER role

        await click(f"emp:{second.id}")
        assert "недоступно" in bot.calls[-1].text
        await click(f"empstatus:{second.id}")
        assert employees.get(second.id).status == "active"
        await click(f"rate:{second.id}")
        assert "недоступно" in bot.calls[-1].text
        await send("📊 Отчёты")
        assert "своих данных" in bot.calls[-1].text
        await click("export:csv")
        assert "недоступно" in bot.calls[-1].text
        await send("/start")
        assert "Ваши данные" in bot.calls[-1].text

        await click("profile:edit")
        await send("Несохранённое имя")
        await send("/cancel")
        assert employees.get(first.id).name == "Иванов Иван Иванович"
        await click("profile:edit")
        await send("Новое ФИО")
        await send("Другой тестовый банк")
        employees.set_status(first.id, "inactive", actor=1001)
        await click("profile:save")
        assert "Нет доступа" in bot.calls[-1].text
        assert employees.get(first.id).name == "Иванов Иван Иванович"
        employees.set_status(first.id, "active", actor=1001)

        await click("profile:edit", group=True)
        assert bot.calls[-1].text == "Нет доступа"
        await click(f"emp:{first.id}", user=1001)
        assert "Иванов Иван Иванович" in bot.calls[-1].text
        assert "СБП +79990000000" in bot.calls[-1].text
        third = create_employee(employees, "Новый сотрудник")
        await click(f"empinvite:{third.id}", user=1001)
        assert "https://t.me/test_bot?start=employee_" in bot.calls[-1].text
        await click(f"empinvite:{third.id}", user=2001)
        assert "недоступно" in bot.calls[-1].text

        await send("/start", user=3001)
        assert "Введите фамилию" in bot.calls[-1].text
        await send("Самостоятельный Сотрудник", user=3001)
        await send("Тестовый банк, СБП +79991112233", user=3001)
        await click("profile:save", user=3001)
        registered = employees.by_telegram(3001)
        assert registered is not None
        assert registered.name == "Самостоятельный Сотрудник"
        assert registered.payment_details == "Тестовый банк, СБП +79991112233"

        with sessions() as session, session.begin():
            session.get(User, 1001).is_active = False
        await send("/start", user=1001)
        assert bot.calls[-1].text == "Нет доступа."
        await bot.session.close()
        await configured_bot.session.close()

    asyncio.run(scenario())
