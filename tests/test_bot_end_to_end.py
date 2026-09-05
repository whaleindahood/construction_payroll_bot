from __future__ import annotations

import asyncio
from datetime import UTC, datetime

from aiogram import Bot
from aiogram.types import Chat, Message, Update
from aiogram.types import User as TelegramUser

from app.config import Settings
from app.db import create_schema, make_session_factory
from app.main import compose


class FakeBot(Bot):
    def __init__(self):
        super().__init__("123456:abcdefghijklmnopqrstuvwxyzABCDE")
        self.calls = []

    async def __call__(self, method, request_timeout=None):
        self.calls.append(method)
        return True


def telegram_message(update_id: int, user_id: int, text: str) -> Update:
    return Update(
        update_id=update_id,
        message=Message(
            message_id=update_id,
            date=datetime.now(UTC),
            chat=Chat(id=user_id, type="private"),
            from_user=TelegramUser(id=user_id, is_bot=False, first_name="Owner"),
            text=text,
        ),
    )


def test_owner_update_runs_end_to_end_and_duplicate_is_ignored(tmp_path):
    database_url = f"sqlite:///{tmp_path / 'bot.db'}"
    create_schema(make_session_factory(database_url))
    configured_bot, dispatcher = compose(
        Settings(
            bot_token="123456:abcdefghijklmnopqrstuvwxyzABCDE",
            owner_ids={1001},
            database_url=database_url,
        )
    )
    bot = FakeBot()
    update = telegram_message(77, 1001, "/start")

    asyncio.run(dispatcher.feed_update(bot, update))
    calls_after_first_delivery = len(bot.calls)
    asyncio.run(dispatcher.feed_update(bot, update))

    assert calls_after_first_delivery == 1
    assert len(bot.calls) == calls_after_first_delivery
    asyncio.run(bot.session.close())
    asyncio.run(configured_bot.session.close())


def test_unknown_telegram_id_is_rejected(tmp_path):
    database_url = f"sqlite:///{tmp_path / 'bot.db'}"
    create_schema(make_session_factory(database_url))
    configured_bot, dispatcher = compose(
        Settings(
            bot_token="123456:abcdefghijklmnopqrstuvwxyzABCDE",
            owner_ids={1001},
            database_url=database_url,
        )
    )
    bot = FakeBot()

    asyncio.run(dispatcher.feed_update(bot, telegram_message(78, 9999, "/start")))

    assert len(bot.calls) == 1
    assert "Попросите владельца" in bot.calls[0].text
    asyncio.run(bot.session.close())
    asyncio.run(configured_bot.session.close())
