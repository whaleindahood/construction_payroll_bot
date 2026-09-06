from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from typing import Any

from aiogram import BaseMiddleware, Bot, Dispatcher
from aiogram.fsm.storage.memory import MemoryStorage, SimpleEventIsolation
from aiogram.types import CallbackQuery, Message, Update
from aiogram.types.base import TelegramObject

from app.bot.common import Services
from app.bot.employee import build_employee_router
from app.bot.object_workflow import build_router
from app.config import Settings
from app.db import make_session_factory
from app.services import (
    AccessService,
    AttendanceService,
    EmployeeService,
    ObjectService,
    PaymentService,
    PayrollService,
    UpdateDedupService,
)


def update_user(event: Update):
    candidate = event.message or event.callback_query or event.edited_message
    return getattr(candidate, "from_user", None)


async def reject(event: Update) -> None:
    candidate: Message | CallbackQuery | None = event.message or event.callback_query
    if isinstance(candidate, CallbackQuery):
        await candidate.answer("Нет доступа", show_alert=True)
    elif isinstance(candidate, Message):
        await candidate.answer("Нет доступа.")


class AccessMiddleware(BaseMiddleware):
    def __init__(self, access: AccessService):
        self.access = access

    async def __call__(
        self,
        handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: dict[str, Any],
    ) -> Any:
        if not isinstance(event, Update):
            return await handler(event, data)
        user = update_user(event)
        candidate = event.message or (event.callback_query and event.callback_query.message)
        if user is None or candidate is None or candidate.chat.type != "private":
            await reject(event)
            return None
        is_owner = self.access.ensure_owner(user.id)
        if user.id in self.access.owner_ids and not is_owner:
            await reject(event)
            return None
        data["is_owner"] = is_owner
        return await handler(event, data)


class UpdateDedupMiddleware(BaseMiddleware):
    def __init__(self, dedup: UpdateDedupService):
        self.dedup = dedup

    async def __call__(
        self,
        handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: dict[str, Any],
    ) -> Any:
        if not isinstance(event, Update):
            return await handler(event, data)
        if not self.dedup.claim(event.update_id):
            return None
        result = await handler(event, data)
        self.dedup.done(event.update_id)
        return result


def compose(settings: Settings) -> tuple[Bot, Dispatcher]:
    if not settings.owner_ids:
        raise RuntimeError("PAYROLL_OWNER_IDS must contain at least one Telegram ID")
    sessions = make_session_factory(settings.database_url)
    employees = EmployeeService(sessions)
    objects = ObjectService(sessions)
    attendance = AttendanceService(sessions)
    payroll = PayrollService(sessions)
    services = Services(
        employees=employees,
        objects=objects,
        attendance=attendance,
        payroll=payroll,
        payments=PaymentService(sessions),
    )
    bot = Bot(settings.bot_token)
    dispatcher = Dispatcher(storage=MemoryStorage(), events_isolation=SimpleEventIsolation())
    dispatcher.update.outer_middleware(UpdateDedupMiddleware(UpdateDedupService(sessions)))
    dispatcher.update.outer_middleware(
        AccessMiddleware(AccessService(sessions, settings.owner_ids))
    )
    dispatcher.include_router(
        build_router(
            services,
            timezone_name=settings.timezone,
        )
    )
    dispatcher.include_router(build_employee_router(employees))
    return bot, dispatcher


async def run() -> None:
    settings = Settings()  # pyright: ignore[reportCallIssue] - values come from the environment
    bot, dispatcher = compose(settings)
    try:
        await dispatcher.start_polling(bot, allowed_updates=dispatcher.resolve_used_update_types())
    finally:
        await bot.session.close()


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    asyncio.run(run())
