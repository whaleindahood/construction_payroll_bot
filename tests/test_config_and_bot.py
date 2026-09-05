from __future__ import annotations

import asyncio

from app.config import Settings
from app.db import create_schema, make_session_factory
from app.main import compose
from app.services import AccessService


def test_owner_ids_are_explicit_numeric_ids(monkeypatch):
    monkeypatch.setenv("PAYROLL_BOT_TOKEN", "123456:abcdefghijklmnopqrstuvwxyzABCDE")
    monkeypatch.setenv("PAYROLL_OWNER_IDS", "1001,1002")
    settings = Settings(_env_file=None)

    assert settings.owner_ids == {1001, 1002}


def test_access_service_never_trusts_username(tmp_path):
    sessions = make_session_factory(f"sqlite:///{tmp_path / 'access.db'}")
    create_schema(sessions)
    access = AccessService(sessions, {1001})

    assert access.ensure_owner(9999) is False
    assert access.ensure_owner(1001) is True


def test_bot_composition_loads_router_without_network():
    settings = Settings(
        bot_token="123456:abcdefghijklmnopqrstuvwxyzABCDE",
        owner_ids={1001},
        database_url="sqlite:///:memory:",
    )

    bot, dispatcher = compose(settings)

    assert dispatcher.sub_routers
    asyncio.run(bot.session.close())
