from __future__ import annotations

from datetime import UTC, datetime

from aiogram import F, Router
from aiogram.filters import Command, CommandObject, MagicData
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, Message, ReplyKeyboardRemove
from aiogram.utils.keyboard import InlineKeyboardBuilder

from app.bot.keyboards import one_button
from app.services import DomainError, EmployeeService, clean_text


class EmployeeProfile(StatesGroup):
    name = State()
    payment_details = State()
    confirm = State()


def actor_id(message: Message) -> int:
    if message.from_user is None:
        raise DomainError("Не удалось определить пользователя.")
    return message.from_user.id


def callback_message(callback: CallbackQuery) -> Message:
    if not isinstance(callback.message, Message):
        raise DomainError("Сообщение с кнопкой недоступно. Откройте /start.")
    return callback.message


def build_employee_router(employees: EmployeeService) -> Router:
    router = Router(name="employee_profile")
    router.message.filter(MagicData(~F.is_owner))
    router.callback_query.filter(MagicData(~F.is_owner))

    async def show_profile(message: Message, telegram_id: int) -> None:
        employee = employees.by_telegram(telegram_id)
        if employee is None:
            await message.answer(
                "Нет доступа к карточке. Попросите владельца прислать личную ссылку-приглашение.",
                reply_markup=ReplyKeyboardRemove(),
            )
            return
        await message.answer(
            f"Ваши данные\n\nФИО: {employee.name}\n"
            f"Реквизиты:\n{employee.payment_details or 'не заполнены'}",
            reply_markup=one_button("✏️ Заполнить / изменить данные", "profile:edit"),
        )

    async def begin_profile(
        message: Message, state: FSMContext, *, creating: bool = False
    ) -> None:
        await state.update_data(profile_creating=creating)
        await state.set_state(EmployeeProfile.name)
        await message.answer(
            "Введите фамилию, имя и отчество (если есть).\n"
            "Данные будут доступны владельцу для выплаты зарплаты.\n"
            "Для отмены — /cancel.",
            reply_markup=ReplyKeyboardRemove(),
        )

    @router.message(Command("start", "menu", "profile", "cancel"))
    async def start(message: Message, state: FSMContext, command: CommandObject):
        await state.clear()
        if command.command == "start" and command.args:
            if not command.args.startswith("employee_"):
                await message.answer("Некорректное приглашение. Запросите ссылку у владельца.")
                return
            try:
                employees.accept_invite(
                    command.args.removeprefix("employee_"), telegram_id=actor_id(message)
                )
            except DomainError as exc:
                await message.answer(str(exc))
                return
            await begin_profile(message, state)
            return
        telegram_id = actor_id(message)
        if employees.by_telegram(telegram_id) is None:
            await begin_profile(message, state, creating=True)
            return
        await show_profile(message, telegram_id)

    @router.callback_query(F.data == "profile:edit")
    async def edit(callback: CallbackQuery, state: FSMContext):
        await state.clear()
        if employees.by_telegram(callback.from_user.id) is None:
            await callback.answer("Нет доступа к карточке.", show_alert=True)
            return
        await callback.answer()
        await begin_profile(callback_message(callback), state)

    @router.message(EmployeeProfile.name)
    async def name(message: Message, state: FSMContext):
        try:
            value = clean_text(message.text, "ФИО", 200, required=True)
        except DomainError as exc:
            await message.answer(str(exc))
            return
        await state.update_data(name=value)
        await state.set_state(EmployeeProfile.payment_details)
        await message.answer(
            "Введите реквизиты одним сообщением (до 1000 символов):\n"
            "• банк и телефон для перевода по СБП;\n"
            "• либо банк и номер карты;\n"
            "• либо банк, номер счёта и БИК.\n\n"
            "Не указывайте PIN, CVV/CVC и коды из SMS.\nДля отмены — /cancel."
        )

    @router.message(EmployeeProfile.payment_details)
    async def payment_details(message: Message, state: FSMContext):
        try:
            value = clean_text(message.text, "Реквизиты", 1000, required=True)
        except DomainError as exc:
            await message.answer(str(exc))
            return
        await state.update_data(payment_details=value)
        data = await state.get_data()
        await state.set_state(EmployeeProfile.confirm)
        builder = InlineKeyboardBuilder()
        builder.button(text="✅ Сохранить", callback_data="profile:save")
        builder.button(text="✏️ Изменить", callback_data="profile:edit")
        builder.button(text="Отмена", callback_data="profile:cancel")
        builder.adjust(1)
        await message.answer(
            f"Проверьте данные:\n\nФИО: {data['name']}\nРеквизиты:\n{value}",
            reply_markup=builder.as_markup(),
        )

    @router.callback_query(EmployeeProfile.confirm, F.data == "profile:save")
    async def save(callback: CallbackQuery, state: FSMContext):
        data = await state.get_data()
        try:
            if data.get("profile_creating"):
                employees.create(
                    name=data["name"],
                    payment_details=data["payment_details"],
                    telegram_id=callback.from_user.id,
                    start_date=datetime.now(UTC).date(),
                    actor=callback.from_user.id,
                )
            else:
                employees.update_own_profile(
                    callback.from_user.id,
                    name=data["name"],
                    payment_details=data["payment_details"],
                )
        except DomainError as exc:
            await state.clear()
            await callback.answer(str(exc), show_alert=True)
            return
        await state.clear()
        await callback.answer("Сохранено")
        message = callback_message(callback)
        await message.answer("Данные сохранены и доступны владельцу.")
        await show_profile(message, callback.from_user.id)

    @router.callback_query(F.data == "profile:cancel")
    async def cancel(callback: CallbackQuery, state: FSMContext):
        await state.clear()
        await callback.answer("Отменено")
        await show_profile(callback_message(callback), callback.from_user.id)

    @router.callback_query()
    async def unavailable_callback(callback: CallbackQuery):
        await callback.answer("Действие недоступно. Откройте /start.", show_alert=True)

    @router.message()
    async def unavailable_message(message: Message):
        await message.answer("Для просмотра и изменения своих данных отправьте /start.")

    return router
