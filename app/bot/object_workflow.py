from __future__ import annotations

import uuid
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

from aiogram import F, Router
from aiogram.filters import Command, ExceptionTypeFilter, MagicData
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import BufferedInputFile, CallbackQuery, ErrorEvent, Message
from aiogram.utils.deep_linking import create_start_link
from aiogram.utils.keyboard import InlineKeyboardBuilder

from app.bot import keyboards as kb
from app.bot.handlers import Services, answer_long, parse_date
from app.reports import table_csv, table_xlsx
from app.services import DomainError, clean_text, money
from app.teams import TeamService


class ObjectForm(StatesGroup):
    name = State()
    address = State()
    date = State()
    custom_date = State()


class PersonForm(StatesGroup):
    name = State()
    phone = State()
    details = State()
    telegram = State()
    rate = State()
    confirm = State()


class ShiftForm(StatesGroup):
    date = State()
    custom_date = State()
    people = State()
    confirm = State()


class EditForm(StatesGroup):
    person = State()
    rate = State()
    object = State()


class DeleteForm(StatesGroup):
    confirm = State()


def buttons(*items):
    builder = InlineKeyboardBuilder()
    for label, callback in items:
        builder.button(text=label, callback_data=callback)
    builder.adjust(1)
    return builder.as_markup()


def optional(value, field, limit):
    return clean_text(None if value == "-" else value, field, limit)


def build_router(services: Services, *, timezone_name: str, default_currency: str) -> Router:
    router = Router(name="object_workflow")
    router.message.filter(MagicData(F.is_owner))
    router.callback_query.filter(MagicData(F.is_owner))
    team = TeamService(services.employees.sessions)

    def today():
        return datetime.now(ZoneInfo(timezone_name)).date()

    async def objects(message, *, deleted=False):
        rows = [
            obj
            for obj in services.objects.list(active_only=False)
            if (obj.status == "archived") == deleted
        ]
        actions = [(obj.name, f"obj:{obj.id}") for obj in rows]
        if deleted:
            actions.append(("← Объекты", "objects"))
        else:
            actions += [("➕ Добавить", "obj:create"), ("🗑 Удаленные объекты", "objects:deleted")]
        await message.answer(
            "Удаленные объекты (можно восстановить):"
            if deleted
            else "Выберите объект или создайте новый:",
            reply_markup=buttons(*actions),
        )

    async def employees(message, *, deleted=False):
        rows = [
            emp
            for emp in services.employees.list(active_only=False)
            if (emp.status == "inactive") == deleted
        ]
        actions = [(emp.name, f"emp:{emp.id}") for emp in rows]
        if deleted:
            actions.append(("← Сотрудники", "employees"))
        else:
            actions += [
                ("➕ Добавить", "emp:create"),
                ("🗑 Удаленные сотрудники", "employees:deleted"),
            ]
        await message.answer(
            "Удаленные сотрудники (можно восстановить):" if deleted else "База сотрудников:",
            reply_markup=buttons(*actions),
        )

    async def object_card(message, object_id):
        obj = services.objects.get(object_id)
        rows = team.roster(object_id)
        text = [
            f"🏗 {obj.name}",
            f"Адрес: {obj.address or '—'}",
            f"Начало: {obj.start_date:%d.%m.%Y}",
            f"Статус: { {'active': 'в работе', 'completed': 'завершен', 'archived': 'удален'}[obj.status] }",
            f"Описание: {obj.description or '—'}",
            f"Примечание: {obj.comment or '—'}",
            "",
            "Сотрудники и смены на этом объекте:",
        ]
        text.extend(
            f"{employee.name} — {count} смен"
            + (" (неактивен)" if employee.status != "active" else "")
            for _, employee, count in rows
        )
        if not rows:
            text.append("Сотрудников пока нет. Добавьте из базы или создайте карточку.")
        actions = []
        if obj.status == "active":
            actions += [
                ("📅 Отметить смену", f"shift:{obj.id}"),
                ("➕ Добавить сотрудника", "team:add"),
            ]
        else:
            text.append("Объект закрыт для новых смен.")
        actions += [
            ("👷 Состав и история смен", "team:list"),
            ("📊 Скачать счетчики смен", "team:export"),
        ]
        if obj.status == "archived":
            actions.append(("♻️ Восстановить объект", f"restore:obj:{obj.id}"))
        else:
            actions += [
                ("✏️ Изменить объект", "object:edit"),
                ("🗑 Удалить объект", f"delete:obj:{obj.id}"),
            ]
        actions.append(("← Объекты", "objects"))
        await answer_long(message, "\n".join(text), reply_markup=buttons(*actions))

    async def employee_card(message, employee_id):
        employee = services.employees.get(employee_id)
        lines = [
            f"👷 {employee.name}",
            f"Телефон: {employee.phone or '—'}",
            f"Telegram ID: {employee.telegram_id or 'не привязан'}",
            f"Реквизиты:\n{employee.payment_details or 'не заполнены'}",
            f"Начало работы: {employee.start_date:%d.%m.%Y}",
            f"Примечание: {employee.comment or '—'}",
            f"Статус: {'активен' if employee.status == 'active' else 'удален'}",
            "",
            "Смены по объектам:",
        ]
        lines.extend(
            f"{obj.name} — {count} смен" for obj, count in team.employee_objects(employee.id)
        )
        actions = (
            [
                ("✏️ Изменить карточку", f"empedit:{employee.id}"),
                ("🗑 Удалить сотрудника", f"delete:emp:{employee.id}"),
            ]
            if employee.status == "active"
            else [("♻️ Восстановить сотрудника", f"restore:emp:{employee.id}")]
        )
        if employee.telegram_id is None and employee.status == "active":
            actions.append(("🔗 Пригласить сотрудника", f"empinvite:{employee.id}"))
        actions.append(("← Назад", "cancel"))
        await answer_long(message, "\n".join(lines), reply_markup=buttons(*actions))

    async def back(message, state):
        data = await state.get_data()
        await state.clear()
        if data.get("object_id"):
            await state.update_data(object_id=data["object_id"])
            await object_card(message, data["object_id"])
        else:
            await objects(message)

    @router.error(ExceptionTypeFilter(DomainError))
    async def domain_error(event: ErrorEvent):
        if event.update.callback_query:
            await event.update.callback_query.answer(str(event.exception)[:190], show_alert=True)
        elif event.update.message:
            await event.update.message.answer(str(event.exception))
        return True

    @router.message(Command("start", "menu"))
    async def start(message: Message, state: FSMContext):
        await state.clear()
        await message.answer(
            "Учет смен по объектам.\nОткройте «🏗 Объекты», выберите объект и отметьте вышедших сотрудников.",
            reply_markup=kb.MAIN,
        )

    @router.message(Command("cancel"))
    async def cancel_message(message: Message, state: FSMContext):
        await back(message, state)

    @router.callback_query(F.data == "cancel")
    async def cancel(callback: CallbackQuery, state: FSMContext):
        await callback.answer("Отменено")
        await back(callback.message, state)

    @router.message(F.text.in_({"🏗 Объекты", "📊 Смены", "📅 Рабочие дни", "📊 Отчёты"}))
    async def object_menu(message: Message, state: FSMContext):
        await state.clear()
        await objects(message)

    @router.callback_query(F.data == "objects")
    async def object_list(callback: CallbackQuery, state: FSMContext):
        await state.clear()
        await callback.answer()
        await objects(callback.message)

    @router.callback_query(F.data.in_({"objects:deleted", "employees:deleted", "employees"}))
    async def card_lists(callback: CallbackQuery, state: FSMContext):
        await state.clear()
        await callback.answer()
        if callback.data == "objects:deleted":
            await objects(callback.message, deleted=True)
        else:
            await employees(callback.message, deleted=callback.data == "employees:deleted")

    @router.callback_query(F.data.regexp(r"^delete:(obj|emp):[0-9a-f-]{36}$"))
    async def delete_preview(callback: CallbackQuery, state: FSMContext):
        _, kind, entity_id = callback.data.split(":", 2)
        if kind not in {"obj", "emp"}:
            raise DomainError("Неизвестная карточка.")
        entity = (services.objects if kind == "obj" else services.employees).get(entity_id)
        await state.set_state(DeleteForm.confirm)
        await state.update_data(delete_kind=kind, delete_id=entity_id)
        await callback.answer()
        detail = (
            "Объект исчезнет из основного списка, новые смены на нем будут недоступны. "
            if kind == "obj"
            else "Сотрудник исчезнет из основной базы и выбора на смену. Его доступ к боту будет закрыт на всех объектах. "
        )
        await callback.message.answer(
            f"Удалить «{entity.name}»?\n\n{detail}История смен и выплат сохранится. Карточку можно восстановить в списке удаленных.",
            reply_markup=buttons(
                ("🗑 Подтвердить удаление", f"deleteok:{kind}:{entity_id}"),
                ("Отмена", "delete:cancel"),
            ),
        )

    @router.callback_query(DeleteForm.confirm, F.data.startswith("deleteok:"))
    async def delete_confirm(callback: CallbackQuery, state: FSMContext):
        _, kind, entity_id = callback.data.split(":", 2)
        data = await state.get_data()
        if (kind, entity_id) != (data.get("delete_kind"), data.get("delete_id")):
            raise DomainError("Это подтверждение устарело. Откройте нужную карточку заново.")
        if kind == "obj":
            services.objects.set_status(entity_id, "archived", actor=callback.from_user.id)
        else:
            services.employees.set_status(entity_id, "inactive", actor=callback.from_user.id)
        await state.clear()
        await callback.answer("Карточка удалена")
        if kind == "obj":
            await objects(callback.message)
        else:
            await employees(callback.message)

    @router.callback_query(F.data == "delete:cancel")
    async def delete_cancel(callback: CallbackQuery, state: FSMContext):
        data = await state.get_data()
        await state.set_state(None)
        await callback.answer("Удаление отменено")
        if data.get("delete_kind") == "emp":
            await employee_card(callback.message, data["delete_id"])
        elif data.get("delete_kind") == "obj":
            await state.update_data(object_id=data["delete_id"])
            await object_card(callback.message, data["delete_id"])

    @router.callback_query(F.data.startswith("restore:"))
    async def restore(callback: CallbackQuery, state: FSMContext):
        _, kind, entity_id = callback.data.split(":", 2)
        if kind not in {"obj", "emp"}:
            raise DomainError("Неизвестная карточка.")
        service = services.objects if kind == "obj" else services.employees
        service.set_status(entity_id, "active", actor=callback.from_user.id)
        await state.clear()
        await callback.answer("Карточка восстановлена")
        if kind == "obj":
            await state.update_data(object_id=entity_id)
            await object_card(callback.message, entity_id)
        else:
            await employee_card(callback.message, entity_id)

    @router.callback_query(F.data == "obj:create")
    async def create_object(callback: CallbackQuery, state: FSMContext):
        await state.clear()
        await state.set_state(ObjectForm.name)
        await callback.answer()
        await callback.message.answer("Название объекта:")

    @router.message(ObjectForm.name)
    async def object_name(message: Message, state: FSMContext):
        await state.update_data(name=clean_text(message.text, "Название", 200, required=True))
        await state.set_state(ObjectForm.address)
        await message.answer("Адрес объекта или «-», чтобы пропустить:")

    @router.message(ObjectForm.address)
    async def object_address(message: Message, state: FSMContext):
        await state.update_data(address=optional(message.text, "Адрес", 1000))
        await state.set_state(ObjectForm.date)
        await message.answer("Дата начала работ:", reply_markup=kb.dates("objectdate"))

    async def save_object(message, state, start_date, actor):
        data = await state.get_data()
        obj = services.objects.create(
            name=data["name"], address=data["address"], start_date=start_date, actor=actor
        )
        await state.clear()
        await state.update_data(object_id=obj.id)
        await object_card(message, obj.id)

    @router.callback_query(ObjectForm.date, F.data.startswith("objectdate:"))
    async def object_date(callback: CallbackQuery, state: FSMContext):
        choice = callback.data.rsplit(":", 1)[1]
        if choice == "custom":
            await state.set_state(ObjectForm.custom_date)
            await callback.message.answer("Введите дату ДД.ММ.ГГГГ:")
        else:
            await save_object(
                callback.message,
                state,
                today() - timedelta(days=choice == "yesterday"),
                callback.from_user.id,
            )
        await callback.answer()

    @router.message(ObjectForm.custom_date)
    async def object_custom_date(message: Message, state: FSMContext):
        await save_object(message, state, parse_date(message.text), message.from_user.id)

    @router.callback_query(F.data.startswith("obj:"))
    async def open_object(callback: CallbackQuery, state: FSMContext):
        object_id = callback.data.split(":", 1)[1]
        services.objects.get(object_id)
        await state.clear()
        await state.update_data(object_id=object_id)
        await callback.answer()
        await object_card(callback.message, object_id)

    @router.callback_query(F.data == "object:edit")
    async def edit_object(callback: CallbackQuery, state: FSMContext):
        data = await state.get_data()
        if not data.get("object_id"):
            raise DomainError("Сначала откройте объект.")
        await callback.answer()
        await callback.message.answer(
            "Что изменить?",
            reply_markup=buttons(
                ("Название", "objectfield:name"),
                ("Адрес", "objectfield:address"),
                ("Дата начала работ", "objectfield:start_date"),
                ("Описание", "objectfield:description"),
                ("Примечание", "objectfield:comment"),
                ("Завершить / возобновить работы", "object:status"),
                ("← Назад", "cancel"),
            ),
        )

    @router.callback_query(F.data.startswith("objectfield:"))
    async def object_edit_field(callback: CallbackQuery, state: FSMContext):
        if not (await state.get_data()).get("object_id"):
            raise DomainError("Сначала откройте объект.")
        await state.update_data(edit_field=callback.data.split(":")[1])
        await state.set_state(EditForm.object)
        await callback.answer()
        await callback.message.answer(
            "Введите дату ДД.ММ.ГГГГ:"
            if callback.data.endswith(":start_date")
            else "Введите новое значение. «-» очищает необязательное поле:"
        )

    @router.message(EditForm.object)
    async def object_edit_value(message: Message, state: FSMContext):
        data = await state.get_data()
        obj = services.objects.get(data["object_id"])
        values = {
            "name": obj.name,
            "address": obj.address,
            "description": obj.description,
            "comment": obj.comment,
        }
        if data["edit_field"] not in {"name", "address", "start_date", "description", "comment"}:
            raise DomainError("Неизвестное поле.")
        values[data["edit_field"]] = (
            parse_date(message.text)
            if data["edit_field"] == "start_date"
            else optional(message.text, "Значение", 4000)
        )
        services.objects.update(obj.id, **values, actor=message.from_user.id)
        await back(message, state)

    @router.callback_query(F.data == "object:status")
    async def object_status(callback: CallbackQuery, state: FSMContext):
        data = await state.get_data()
        obj = services.objects.get(data.get("object_id", ""))
        services.objects.set_status(
            obj.id, "completed" if obj.status == "active" else "active", actor=callback.from_user.id
        )
        await callback.answer("Статус изменен")
        await back(callback.message, state)

    @router.message(F.text == "👷 Сотрудники")
    async def employee_menu(message: Message, state: FSMContext):
        await state.clear()
        await employees(message)

    @router.callback_query(F.data == "team:add")
    async def add_menu(callback: CallbackQuery, state: FSMContext):
        data = await state.get_data()
        if not data.get("object_id"):
            raise DomainError("Сначала откройте объект.")
        await callback.answer()
        await callback.message.answer(
            "Добавить сотрудника на объект:",
            reply_markup=buttons(
                ("Выбрать из базы", "team:existing"),
                ("Создать полную карточку", "emp:create"),
                ("← Назад", "cancel"),
            ),
        )

    @router.callback_query(F.data == "team:existing")
    async def existing_people(callback: CallbackQuery, state: FSMContext):
        data = await state.get_data()
        if not data.get("object_id"):
            raise DomainError("Сначала откройте объект.")
        await callback.answer()
        available = team.available(data["object_id"])
        await callback.message.answer(
            "Выберите сотрудника:"
            if available
            else "Все сотрудники базы уже добавлены. Можно создать новую карточку.",
            reply_markup=kb.entity_list(available, "attach", create_callback="emp:create"),
        )

    async def rate_prompt(message, state):
        await state.set_state(PersonForm.rate)
        await message.answer(
            "Стоимость смены этого сотрудника на этом объекте, в рублях.\nВведите сумму или «-», чтобы вести только счетчик смен:"
        )

    @router.callback_query(F.data.startswith("attach:"))
    async def attach_person(callback: CallbackQuery, state: FSMContext):
        employee = services.employees.get(callback.data.split(":", 1)[1])
        data = await state.get_data()
        if not data.get("object_id"):
            raise DomainError("Сначала откройте объект.")
        await state.update_data(existing_employee_id=employee.id, person_name=employee.name)
        await callback.answer()
        await rate_prompt(callback.message, state)

    @router.callback_query(F.data == "emp:create")
    async def create_person(callback: CallbackQuery, state: FSMContext):
        data = await state.get_data()
        await state.set_data(
            {key: data[key] for key in ("object_id", "work_date", "employee_ids") if key in data}
        )
        await state.set_state(PersonForm.name)
        await callback.answer()
        await callback.message.answer(
            "Фамилия, имя и отчество сотрудника (если есть):\nДля отмены — /cancel."
        )

    @router.message(PersonForm.name)
    async def person_name(message: Message, state: FSMContext):
        await state.update_data(person_name=clean_text(message.text, "ФИО", 200, required=True))
        await state.set_state(PersonForm.phone)
        await message.answer("Телефон сотрудника или «-», чтобы пропустить:")

    @router.message(PersonForm.phone)
    async def person_phone(message: Message, state: FSMContext):
        await state.update_data(person_phone=optional(message.text, "Телефон", 50))
        await state.set_state(PersonForm.details)
        await message.answer(
            "Реквизиты для выплаты (банк и телефон для СБП, номер карты или счет) либо «-», чтобы сотрудник заполнил их позже:"
        )

    @router.message(PersonForm.details)
    async def person_details(message: Message, state: FSMContext):
        await state.update_data(person_details=optional(message.text, "Реквизиты", 1000))
        await state.set_state(PersonForm.telegram)
        await message.answer(
            "Числовой Telegram ID сотрудника либо «-», чтобы пригласить его по личной ссылке позже:"
        )

    @router.message(PersonForm.telegram)
    async def person_telegram(message: Message, state: FSMContext):
        raw = optional(message.text, "Telegram ID", 20)
        if raw is not None and (not raw.isascii() or not raw.isdigit() or not 0 < int(raw) < 2**63):
            raise DomainError("Введите положительный числовой Telegram ID или «-».")
        await state.update_data(person_telegram=int(raw) if raw else None)
        data = await state.get_data()
        if data.get("object_id"):
            await rate_prompt(message, state)
        else:
            await person_preview(message, state)

    async def person_preview(message, state):
        data = await state.get_data()
        lines = ["Проверьте карточку:", data["person_name"]]
        if not data.get("existing_employee_id"):
            lines += [
                f"Телефон: {data.get('person_phone') or '—'}",
                f"Реквизиты: {data.get('person_details') or 'не заполнены'}",
                f"Telegram ID: {data.get('person_telegram') or 'по приглашению'}",
            ]
        if data.get("object_id"):
            lines += [
                f"Объект: {services.objects.get(data['object_id']).name}",
                f"Стоимость смены: {data.get('shift_rate') or 'не указана'}",
            ]
        await state.set_state(PersonForm.confirm)
        await message.answer(
            "\n".join(lines),
            reply_markup=buttons(("✅ Сохранить", "person:save"), ("Отмена", "cancel")),
        )

    @router.message(PersonForm.rate)
    async def person_rate(message: Message, state: FSMContext):
        value = optional(message.text, "Стоимость", 30)
        await state.update_data(shift_rate=str(money(value)) if value is not None else None)
        await person_preview(message, state)

    @router.callback_query(PersonForm.confirm, F.data == "person:save")
    async def person_save(callback: CallbackQuery, state: FSMContext):
        data = await state.get_data()
        if data.get("existing_employee_id"):
            employee_id = data["existing_employee_id"]
            team.add(
                data["object_id"],
                employee_id,
                shift_rate=data.get("shift_rate"),
                actor=callback.from_user.id,
            )
        else:
            employee = services.employees.create(
                name=data["person_name"],
                phone=data.get("person_phone"),
                payment_details=data.get("person_details"),
                telegram_id=data.get("person_telegram"),
                currency="RUB",
                start_date=date.fromisoformat(data["work_date"])
                if data.get("work_date")
                else today(),
                actor=callback.from_user.id,
                object_id=data.get("object_id"),
                shift_rate=data.get("shift_rate"),
            )
            employee_id = employee.id
        await callback.answer("Сохранено")
        await state.clear()
        if data.get("object_id"):
            await state.update_data(object_id=data["object_id"])
            if data.get("work_date"):
                selected = list(dict.fromkeys([*data.get("employee_ids", []), employee_id]))
                await state.update_data(work_date=data["work_date"], employee_ids=selected)
                await shift_picker(callback.message, state)
            else:
                await object_card(callback.message, data["object_id"])
        else:
            await employee_card(callback.message, employee_id)

    @router.callback_query(F.data.startswith("emp:"))
    async def open_employee(callback: CallbackQuery, state: FSMContext):
        await state.set_state(None)
        await callback.answer()
        await employee_card(callback.message, callback.data.split(":", 1)[1])

    @router.callback_query(F.data.startswith("empinvite:"))
    async def invite(callback: CallbackQuery):
        token = services.employees.create_invite(
            callback.data.split(":", 1)[1], actor=callback.from_user.id
        )
        link = await create_start_link(callback.bot, f"employee_{token}")
        await callback.answer()
        await callback.message.answer(
            f"Отправьте ссылку лично сотруднику:\n{link}\n\nСсылка одноразовая, действует 7 дней. Новая ссылка отменяет предыдущую."
        )

    @router.callback_query(F.data.startswith("empedit:"))
    async def edit_person(callback: CallbackQuery, state: FSMContext):
        employee_id = callback.data.split(":", 1)[1]
        services.employees.get(employee_id)
        await state.update_data(edit_employee_id=employee_id)
        await callback.answer()
        await callback.message.answer(
            "Что изменить?",
            reply_markup=buttons(
                ("ФИО", "personfield:name"),
                ("Телефон", "personfield:phone"),
                ("Реквизиты", "personfield:payment_details"),
                ("Telegram ID", "personfield:telegram_id"),
                ("Дата начала работы", "personfield:start_date"),
                ("Примечание", "personfield:comment"),
                ("← Назад", "cancel"),
            ),
        )

    @router.callback_query(F.data.startswith("personfield:"))
    async def edit_person_field(callback: CallbackQuery, state: FSMContext):
        await state.update_data(edit_field=callback.data.split(":")[1])
        await state.set_state(EditForm.person)
        await callback.answer()
        await callback.message.answer(
            "Введите дату ДД.ММ.ГГГГ:"
            if callback.data.endswith(":start_date")
            else "Введите новое значение. «-» очищает поле:"
        )

    @router.message(EditForm.person)
    async def edit_person_value(message: Message, state: FSMContext):
        data = await state.get_data()
        employee = services.employees.get(data.get("edit_employee_id", ""))
        field = data["edit_field"]
        if field not in {
            "name",
            "phone",
            "payment_details",
            "telegram_id",
            "start_date",
            "comment",
        }:
            raise DomainError("Неизвестное поле.")
        value = (
            parse_date(message.text)
            if field == "start_date"
            else optional(message.text, "Значение", 4000)
        )
        if field == "telegram_id" and value is not None:
            if not value.isascii() or not value.isdigit() or not 0 < int(value) < 2**63:
                raise DomainError("Введите положительный числовой Telegram ID или «-».")
            value = int(value)
        values = {
            "name": employee.name,
            "phone": employee.phone,
            "payment_details": employee.payment_details,
            "telegram_id": employee.telegram_id,
            "comment": employee.comment,
        }
        values[field] = value if field != "payment_details" else (value or "")
        services.employees.update(employee.id, **values, actor=message.from_user.id)
        await state.set_state(None)
        await employee_card(message, employee.id)

    @router.callback_query(F.data == "team:list")
    async def roster(callback: CallbackQuery, state: FSMContext):
        data = await state.get_data()
        obj = services.objects.get(data.get("object_id", ""))
        rows = team.roster(obj.id)
        await callback.answer()
        await callback.message.answer(
            f"Состав объекта «{obj.name}». Выберите сотрудника:",
            reply_markup=buttons(
                *[
                    (f"{emp.name} — {count} смен", f"member:{member.id}")
                    for member, emp, count in rows
                ],
                ("← Объект", "cancel"),
            ),
        )

    @router.callback_query(F.data.regexp(r"^member:[0-9a-f-]{36}$"))
    async def member_card(callback: CallbackQuery, state: FSMContext):
        member = team.get(callback.data.split(":", 1)[1])
        employee = services.employees.get(member.employee_id)
        obj = services.objects.get(member.object_id)
        count = next(count for row, _, count in team.roster(obj.id) if row.id == member.id)
        await state.clear()
        await state.update_data(object_id=obj.id, member_id=member.id)
        await callback.answer()
        await callback.message.answer(
            f"{employee.name}\nОбъект: {obj.name}\nСмен: {count}\nСтоимость смены: {member.shift_rate or 'не указана'}",
            reply_markup=buttons(
                ("📋 Карточка сотрудника", f"emp:{employee.id}"),
                ("📅 История смен", "member:history"),
                ("Изменить стоимость смены", "member:rate"),
                ("← Объект", "cancel"),
            ),
        )

    @router.callback_query(F.data == "member:rate")
    async def member_rate(callback: CallbackQuery, state: FSMContext):
        data = await state.get_data()
        team.get(data.get("member_id", ""))
        await state.set_state(EditForm.rate)
        await callback.answer()
        await callback.message.answer(
            "Новая стоимость смены на этом объекте, в рублях, или «-». Она будет применяться при записи новых смен; сохраненные смены не пересчитываются."
        )

    @router.message(EditForm.rate)
    async def save_rate(message: Message, state: FSMContext):
        data = await state.get_data()
        team.set_rate(
            data["member_id"], optional(message.text, "Стоимость", 30), actor=message.from_user.id
        )
        await back(message, state)

    @router.callback_query(F.data.regexp(r"^shift:[0-9a-f-]{36}$"))
    async def shift_start(callback: CallbackQuery, state: FSMContext):
        obj = services.objects.get(callback.data.split(":", 1)[1])
        if obj.status != "active":
            raise DomainError("Объект закрыт для новых смен.")
        await state.clear()
        await state.update_data(object_id=obj.id, employee_ids=[])
        await state.set_state(ShiftForm.date)
        await callback.answer()
        await callback.message.answer(
            f"{obj.name}\nЗа какой день отметить смену?", reply_markup=kb.dates("shiftdate")
        )

    async def shift_picker(message, state):
        data = await state.get_data()
        obj = services.objects.get(data["object_id"])
        work_date = date.fromisoformat(data["work_date"])
        if work_date > today() or work_date < obj.start_date:
            raise DomainError("Дата смены должна быть между началом объекта и сегодняшним днем.")
        recorded = {row.employee_id for row in team.day(obj.id, work_date)}
        rows = [
            (member, emp, count)
            for member, emp, count in team.roster(obj.id)
            if emp.status == "active" and emp.start_date <= work_date
        ]
        selected = set(data.get("employee_ids", [])) & {emp.id for _, emp, _ in rows} - recorded
        await state.update_data(employee_ids=sorted(selected))
        actions = []
        for _, employee, _ in rows:
            mark = "✅" if employee.id in recorded else "☑" if employee.id in selected else "☐"
            actions.append(
                (
                    f"{mark} {employee.name}",
                    "shifted" if employee.id in recorded else f"toggle:{employee.id}",
                )
            )
        actions += [
            ("✅ Сохранить выбранных", "shift:preview"),
            ("➕ Добавить сотрудника", "team:add"),
            ("← Объект", "cancel"),
        ]
        await state.set_state(ShiftForm.people)
        await message.answer(
            f"{obj.name} · {work_date:%d.%m.%Y}\nОтметьте пришедших. ✅ — смена уже записана.\nЗа каждого выбранного сотрудника добавится 1 смена.",
            reply_markup=buttons(*actions),
        )

    @router.callback_query(ShiftForm.date, F.data.startswith("shiftdate:"))
    async def shift_date(callback: CallbackQuery, state: FSMContext):
        choice = callback.data.rsplit(":", 1)[1]
        if choice == "custom":
            await state.set_state(ShiftForm.custom_date)
            await callback.message.answer("Введите дату ДД.ММ.ГГГГ:")
        else:
            await state.update_data(
                work_date=(today() - timedelta(days=choice == "yesterday")).isoformat()
            )
            await shift_picker(callback.message, state)
        await callback.answer()

    @router.message(ShiftForm.custom_date)
    async def shift_custom_date(message: Message, state: FSMContext):
        await state.update_data(work_date=parse_date(message.text).isoformat())
        await shift_picker(message, state)

    @router.callback_query(ShiftForm.people, F.data.startswith("toggle:"))
    async def toggle(callback: CallbackQuery, state: FSMContext):
        data = await state.get_data()
        employee_id = callback.data.split(":", 1)[1]
        allowed = {emp.id for _, emp, _ in team.roster(data["object_id"]) if emp.status == "active"}
        if employee_id not in allowed:
            raise DomainError("Сотрудник не входит в состав объекта.")
        selected = set(data.get("employee_ids", []))
        selected.symmetric_difference_update({employee_id})
        await state.update_data(employee_ids=sorted(selected))
        await callback.answer()
        # Replace the picker rather than adding one message per selected employee.
        markup = callback.message.reply_markup.model_copy(
            update={
                "inline_keyboard": [
                    [
                        button.model_copy(
                            update={
                                "text": ("☑" if employee_id in selected else "☐") + button.text[1:]
                            }
                        )
                        if button.callback_data == callback.data
                        else button
                        for button in row
                    ]
                    for row in callback.message.reply_markup.inline_keyboard
                ]
            }
        )
        await callback.message.edit_reply_markup(reply_markup=markup)

    @router.callback_query(F.data == "shifted")
    async def already_recorded(callback: CallbackQuery):
        await callback.answer(
            "Смена уже записана. Отменить ошибочную отметку можно в истории сотрудника.",
            show_alert=True,
        )

    @router.callback_query(ShiftForm.people, F.data == "shift:preview")
    async def shift_preview(callback: CallbackQuery, state: FSMContext):
        data = await state.get_data()
        selected = data.get("employee_ids", [])
        rows = services.attendance.preview(
            employee_ids=selected,
            object_id=data["object_id"],
            work_date=date.fromisoformat(data["work_date"]),
            coefficient="1",
            require_assignment=True,
        )
        await state.update_data(operation_key=f"shift:{uuid.uuid4()}")
        await state.set_state(ShiftForm.confirm)
        await callback.answer()
        await answer_long(
            callback.message,
            f"Записать смены за {date.fromisoformat(data['work_date']):%d.%m.%Y}?\n"
            + "\n".join(f"{row.employee_name} — +1 смена" for row in rows),
            reply_markup=buttons(
                ("✅ Подтвердить", "shift:save"),
                ("Изменить выбор", "shift:change"),
                ("Отмена", "cancel"),
            ),
        )

    @router.callback_query(ShiftForm.confirm, F.data == "shift:change")
    async def shift_change(callback: CallbackQuery, state: FSMContext):
        await callback.answer()
        await shift_picker(callback.message, state)

    @router.callback_query(ShiftForm.confirm, F.data == "shift:save")
    async def shift_save(callback: CallbackQuery, state: FSMContext):
        data = await state.get_data()
        if date.fromisoformat(data["work_date"]) > today():
            raise DomainError("Нельзя отметить будущую смену.")
        rows = services.attendance.create_bulk(
            employee_ids=data["employee_ids"],
            object_id=data["object_id"],
            work_date=date.fromisoformat(data["work_date"]),
            coefficient="1",
            require_assignment=True,
            actor=callback.from_user.id,
            operation_key=data["operation_key"],
        )
        await callback.answer(f"Смен записано: {len(rows)}")
        await back(callback.message, state)

    @router.callback_query(F.data == "member:history")
    async def member_history(callback: CallbackQuery, state: FSMContext):
        data = await state.get_data()
        member = team.get(data.get("member_id", ""))
        page = int(data.get("history_offset", 0))
        rows = team.history(member.object_id, member.employee_id, offset=page)
        await callback.answer()
        await callback.message.answer(
            "История смен. Нажмите дату для отмены ошибочной отметки:"
            if rows
            else "Записей больше нет.",
            reply_markup=buttons(
                *[(f"{row.work_date:%d.%m.%Y}", f"undo:{row.id}") for row in rows],
                ("Следующие 20", "history:next"),
                ("← Объект", "cancel"),
            ),
        )

    @router.callback_query(F.data == "history:next")
    async def history_next(callback: CallbackQuery, state: FSMContext):
        data = await state.get_data()
        await state.update_data(history_offset=int(data.get("history_offset", 0)) + 20)
        await member_history(callback, state)

    @router.callback_query(F.data.regexp(r"^undo:[0-9a-f-]{36}$"))
    async def undo_preview(callback: CallbackQuery, state: FSMContext):
        row = services.attendance.get(callback.data.split(":", 1)[1])
        await state.update_data(undo_id=row.id)
        employee = services.employees.get(row.employee_id)
        obj = services.objects.get(row.object_id)
        await callback.answer()
        await callback.message.answer(
            f"Отменить смену {employee.name} на объекте «{obj.name}» за {row.work_date:%d.%m.%Y}?",
            reply_markup=buttons(("Подтвердить отмену смены", "undo:confirm"), ("Назад", "cancel")),
        )

    @router.callback_query(F.data == "undo:confirm")
    async def undo_confirm(callback: CallbackQuery, state: FSMContext):
        data = await state.get_data()
        if not data.get("undo_id"):
            raise DomainError("Сначала выберите смену в истории.")
        services.attendance.void(
            data["undo_id"], actor=callback.from_user.id, reason="Исправление отметки владельцем"
        )
        await callback.answer("Смена отменена")
        await back(callback.message, state)

    @router.callback_query(F.data == "team:export")
    async def export_menu(callback: CallbackQuery):
        await callback.answer()
        await callback.message.answer(
            "Счетчики смен по сотрудникам за все время на выбранном объекте:",
            reply_markup=buttons(("CSV", "teamcsv"), ("XLSX", "teamxlsx")),
        )

    @router.callback_query(F.data.in_({"teamcsv", "teamxlsx"}))
    async def export(callback: CallbackQuery, state: FSMContext):
        data = await state.get_data()
        obj = services.objects.get(data.get("object_id", ""))
        rows = [[emp.name, count] for _, emp, count in team.roster(obj.id)]
        kind = "csv" if callback.data == "teamcsv" else "xlsx"
        payload = (
            table_csv(["Сотрудник", "Смены"], rows)
            if kind == "csv"
            else table_xlsx(obj.name, ["Сотрудник", "Смены"], rows)
        )
        await callback.answer()
        await callback.message.answer_document(
            BufferedInputFile(payload, filename=f"shifts-{obj.id}.{kind}")
        )

    @router.callback_query()
    async def stale_callback(callback: CallbackQuery):
        await callback.answer(
            "Откройте объект заново через /start: эта кнопка больше не действует.", show_alert=True
        )

    @router.message()
    async def fallback(message: Message):
        await message.answer(
            "Откройте «🏗 Объекты» для работы со сменами или «👷 Сотрудники» для карточек.",
            reply_markup=kb.MAIN,
        )

    return router
