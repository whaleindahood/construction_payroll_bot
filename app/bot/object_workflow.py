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
    details = State()
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


class MembershipForm(StatesGroup):
    confirm = State()


class ObjectPaymentForm(StatesGroup):
    amount = State()
    date = State()
    custom_date = State()
    comment = State()
    confirm = State()
    void = State()


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
            actions.append(("➕ Новый объект", "obj:create"))
            if any(obj.status == "archived" for obj in services.objects.list(active_only=False)):
                actions.append(("Удалённые объекты", "objects:deleted"))
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
            actions.append(("➕ Новый сотрудник", "emp:create"))
            if any(emp.status == "inactive" for emp in services.employees.list(active_only=False)):
                actions.append(("Удалённые сотрудники", "employees:deleted"))
        await message.answer(
            "Удаленные сотрудники (можно восстановить):" if deleted else "База сотрудников:",
            reply_markup=buttons(*actions),
        )

    async def object_card(message, object_id):
        obj = services.objects.get(object_id)
        rows = team.roster(object_id, active_only=True)
        text = [f"🏗 {obj.name}"]
        if obj.address:
            text.append(obj.address)
        text.append(f"Сотрудников: {len(rows)}")
        actions = []
        if obj.status == "active":
            actions += [
                ("📅 Отметить смены", f"shift:{obj.id}"),
            ]
        else:
            text.append("Объект закрыт для новых смен.")
        actions += [
            ("👷 Сотрудники объекта", f"teamopen:{obj.id}"),
            ("📊 Отчёт", f"reportobj:{obj.id}"),
            ("Настройки объекта", f"objsettings:{obj.id}"),
        ]
        actions.append(("← Объекты", "objects"))
        await answer_long(message, "\n".join(text), reply_markup=buttons(*actions))

    async def employee_card(message, employee_id, member_id=None):
        employee = services.employees.get(employee_id)
        lines = [
            f"👷 {employee.name}",
            f"Реквизиты:\n{employee.payment_details or 'не заполнены'}",
        ]
        if employee.phone:
            lines.append(f"Телефон: {employee.phone}")
        if employee.comment:
            lines.append(employee.comment)
        if employee.status != "active":
            lines.append("Карточка удалена")
        lines += ["", "Смены по объектам:"]
        lines.extend(
            f"{obj.name} — {count} смен" for obj, count in team.employee_objects(employee.id)
        )
        actions = (
            [
                ("Изменить данные", f"empedit:{employee.id}"),
            ]
            if employee.status == "active"
            else [("♻️ Восстановить сотрудника", f"restore:emp:{employee.id}")]
        )
        if employee.telegram_id is None and employee.status == "active":
            actions.append(("🔗 Пригласить сотрудника", f"empinvite:{employee.id}"))
        actions.append(("← Назад", f"member:{member_id}" if member_id else "employees"))
        await answer_long(message, "\n".join(lines), reply_markup=buttons(*actions))

    async def back(message, state):
        data = await state.get_data()
        await state.clear()
        if data.get("member_id"):
            await show_member(message, state, data["member_id"])
        elif data.get("object_id"):
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
            "Смены и выплаты. Выберите объект.",
            reply_markup=kb.MAIN,
        )
        await objects(message)

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

    @router.callback_query(F.data.regexp(r"^objsettings:[0-9a-f-]{36}$"))
    @router.callback_query(F.data == "object:edit")
    async def edit_object(callback: CallbackQuery, state: FSMContext):
        data = await state.get_data()
        object_id = (
            callback.data.split(":", 1)[1]
            if callback.data.startswith("objsettings:")
            else data.get("object_id", "")
        )
        obj = services.objects.get(object_id)
        await state.clear()
        await state.update_data(object_id=obj.id)
        await callback.answer()
        await answer_long(
            callback.message,
            f"Настройки: {obj.name}\nНачало работ: {obj.start_date:%d.%m.%Y}\n"
            f"Описание: {obj.description or '—'}\nПримечание: {obj.comment or '—'}\n\nЧто изменить?",
            reply_markup=buttons(
                ("Название", "objectfield:name"),
                ("Адрес", "objectfield:address"),
                ("Дата начала работ", "objectfield:start_date"),
                ("Описание", "objectfield:description"),
                ("Примечание", "objectfield:comment"),
                ("Завершить / возобновить работы", "object:status"),
                ("Восстановить объект", f"restore:obj:{obj.id}")
                if obj.status == "archived"
                else ("Удалить объект", f"delete:obj:{obj.id}"),
                ("← Объект", f"obj:{obj.id}"),
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

    @router.message(F.text.in_({"👷 Сотрудники", "👷 База сотрудников"}))
    async def employee_menu(message: Message, state: FSMContext):
        await state.clear()
        await employees(message)

    @router.callback_query(F.data == "team:add")
    @router.callback_query(F.data == "team:existing")
    async def existing_people(callback: CallbackQuery, state: FSMContext):
        data = await state.get_data()
        if not data.get("object_id"):
            raise DomainError("Сначала откройте объект.")
        await callback.answer()
        available = team.available(data["object_id"])
        await callback.message.answer(
            "Добавить на объект. Выберите сотрудника из базы или создайте нового:"
            if available
            else "Нет сотрудников для добавления. Создайте нового:",
            reply_markup=buttons(
                *[(employee.name, f"attach:{employee.id}") for employee in available],
                ("➕ Новый сотрудник", "emp:create"),
                ("← Назад", "add:back"),
            ),
        )

    @router.callback_query(F.data == "add:back")
    async def add_back(callback: CallbackQuery, state: FSMContext):
        data = await state.get_data()
        await callback.answer()
        if data.get("work_date"):
            await shift_picker(callback.message, state)
        else:
            await show_roster(callback.message, state, data.get("object_id", ""))

    async def rate_prompt(message, state):
        await state.set_state(PersonForm.rate)
        await message.answer(
            "Ставка за смену на этом объекте, в рублях:",
            reply_markup=buttons(("Указать позже", "person:skiprate")),
        )

    @router.callback_query(PersonForm.rate, F.data == "person:skiprate")
    async def skip_person_rate(callback: CallbackQuery, state: FSMContext):
        await state.update_data(shift_rate=None)
        await callback.answer()
        await person_preview(callback.message, state)

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
        await state.set_state(PersonForm.details)
        await message.answer(
            "Реквизиты: банк и телефон для СБП, номер карты или счёт. Можно заполнить позже.",
            reply_markup=buttons(("Заполнить позже", "person:skipdetails")),
        )

    @router.callback_query(PersonForm.details, F.data == "person:skipdetails")
    async def skip_person_details(callback: CallbackQuery, state: FSMContext):
        await state.update_data(person_details=None)
        await callback.answer()
        await after_person_details(callback.message, state)

    @router.message(PersonForm.details)
    async def person_details(message: Message, state: FSMContext):
        await state.update_data(person_details=optional(message.text, "Реквизиты", 1000))
        await after_person_details(message, state)

    async def after_person_details(message, state):
        data = await state.get_data()
        if data.get("object_id"):
            await rate_prompt(message, state)
        else:
            await person_preview(message, state)

    async def person_preview(message, state):
        data = await state.get_data()
        lines = ["Проверьте карточку:", data["person_name"]]
        if not data.get("existing_employee_id"):
            lines.append(f"Реквизиты: {data.get('person_details') or 'заполнят позже'}")
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
        employee_id = callback.data.split(":", 1)[1]
        data = await state.get_data()
        member_id = data.get("member_id")
        if member_id and team.get(member_id).employee_id != employee_id:
            member_id = None
        await employee_card(callback.message, employee_id, member_id)

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
                ("Удалить сотрудника из базы", f"delete:emp:{employee_id}"),
                ("← Назад", f"emp:{employee_id}"),
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
    @router.callback_query(F.data.regexp(r"^team(open|former):[0-9a-f-]{36}$"))
    async def roster(callback: CallbackQuery, state: FSMContext):
        data = await state.get_data()
        object_id = (
            data.get("object_id", "")
            if callback.data == "team:list"
            else callback.data.split(":", 1)[1]
        )
        await callback.answer()
        await show_roster(
            callback.message, state, object_id, former=callback.data.startswith("teamformer:")
        )

    async def show_roster(message, state, object_id, *, former=False):
        obj = services.objects.get(object_id)
        all_rows = team.roster(obj.id)
        rows = [
            (member, emp, count)
            for member, emp, count in all_rows
            if (member.active and emp.status == "active") != former
        ]
        await state.clear()
        await state.update_data(object_id=obj.id)
        actions = [
            (f"{emp.name} · {count} смен", f"member:{member.id}") for member, emp, count in rows
        ]
        if not former and obj.status == "active":
            actions.append(("➕ Добавить сотрудника", "team:add"))
        if former:
            actions.append(("← Сотрудники объекта", f"teamopen:{obj.id}"))
        elif len(rows) != len(all_rows):
            actions.append(("Бывшие сотрудники", f"teamformer:{obj.id}"))
        actions.append(("← Объект", f"obj:{obj.id}"))
        await message.answer(
            f"{'Бывшие сотрудники' if former else 'Сотрудники объекта'} · {obj.name}\n"
            + ("Выберите сотрудника." if rows else "Список пуст."),
            reply_markup=buttons(*actions),
        )

    @router.callback_query(F.data.regexp(r"^member:[0-9a-f-]{36}$"))
    async def member_card(callback: CallbackQuery, state: FSMContext):
        await callback.answer()
        await show_member(callback.message, state, callback.data.split(":", 1)[1])

    async def show_member(message, state, member_id):
        member = team.get(member_id)
        employee = services.employees.get(member.employee_id)
        obj = services.objects.get(member.object_id)
        count = next(count for row, _, count in team.roster(obj.id) if row.id == member.id)
        await state.clear()
        await state.update_data(object_id=obj.id, member_id=member.id)
        summary = services.payroll.summary(employee.id, object_id=obj.id)
        balance = (
            f"Осталось выплатить: {summary.balance} {summary.currency}"
            if summary.balance >= 0
            else f"Аванс: {-summary.balance} {summary.currency}"
        )
        if summary.unrated_shifts:
            balance = (
                f"Смен без ставки: {summary.unrated_shifts}. Итоговый остаток пока не определён."
            )
        await message.answer(
            f"{employee.name}\nОбъект: {obj.name}\n"
            + ("Убран из состава\n" if not member.active else "")
            + f"Смен: {count}\nСтоимость смены: {member.shift_rate or 'не указана'}\n\n"
            f"За всё время на этом объекте:\n"
            f"Начислено{' по сменам со ставкой' if summary.unrated_shifts else ''}: {summary.earned} {summary.currency}\n"
            f"Выплачено: {summary.paid} {summary.currency}\n{balance}",
            reply_markup=buttons(
                ("💵 Записать выплату", f"pay:{member.id}"),
                ("История", f"memberlog:{member.id}"),
                ("Данные и настройки", f"membermore:{member.id}"),
                ("← Сотрудники объекта", f"teamopen:{obj.id}"),
            ),
        )

    @router.callback_query(F.data.regexp(r"^member(log|more):[0-9a-f-]{36}$"))
    async def member_more(callback: CallbackQuery, state: FSMContext):
        member = team.get(callback.data.split(":", 1)[1])
        employee = services.employees.get(member.employee_id)
        obj = services.objects.get(member.object_id)
        await state.clear()
        await state.update_data(object_id=obj.id, member_id=member.id)
        if callback.data.startswith("memberlog:"):
            text = f"История · {employee.name}\n{obj.name}"
            actions = [("Смены", "member:history"), ("Выплаты", f"pays:{member.id}:0")]
        else:
            text = f"{employee.name}\n{obj.name}\n\nРеквизиты:\n{employee.payment_details or 'не заполнены'}"
            actions = [
                ("Ставка за смену", "member:rate"),
                ("Личные данные", f"emp:{employee.id}"),
                (
                    "Убрать с объекта" if member.active else "Вернуть на объект",
                    f"membership:{int(not member.active)}:{member.id}",
                ),
            ]
            if employee.telegram_id is None and employee.status == "active":
                actions.insert(2, ("Пригласить заполнить данные", f"empinvite:{employee.id}"))
        actions.append(("← Назад", f"member:{member.id}"))
        await callback.answer()
        await callback.message.answer(text, reply_markup=buttons(*actions))

    @router.callback_query(F.data.regexp(r"^membership:[01]:[0-9a-f-]{36}$"))
    async def membership_preview(callback: CallbackQuery, state: FSMContext):
        _, active, member_id = callback.data.split(":")
        member = team.get(member_id)
        employee = services.employees.get(member.employee_id)
        obj = services.objects.get(member.object_id)
        token = str(uuid.uuid4())
        await state.clear()
        await state.update_data(
            object_id=obj.id, member_id=member_id, active=active == "1", token=token
        )
        await state.set_state(MembershipForm.confirm)
        await callback.answer()
        await callback.message.answer(
            f"{'Вернуть' if active == '1' else 'Убрать'} {employee.name} "
            f"{'в состав' if active == '1' else 'из состава'} объекта «{obj.name}»?\n"
            "Карточка, ставка, смены и выплаты сохранятся. Другие объекты не изменятся.",
            reply_markup=buttons(
                ("Подтвердить", f"membershipok:{token}"), ("Отмена", f"member:{member_id}")
            ),
        )

    @router.callback_query(MembershipForm.confirm, F.data.startswith("membershipok:"))
    async def membership_save(callback: CallbackQuery, state: FSMContext):
        data = await state.get_data()
        if callback.data.split(":", 1)[1] != data["token"]:
            raise DomainError("Это подтверждение устарело. Откройте карточку заново.")
        team.set_active(data["member_id"], data["active"], actor=callback.from_user.id)
        await callback.answer("Состав изменён")
        await show_member(callback.message, state, data["member_id"])

    @router.callback_query(F.data.regexp(r"^pay:[0-9a-f-]{36}$"))
    async def payment_start(callback: CallbackQuery, state: FSMContext):
        member = team.get(callback.data.split(":", 1)[1])
        employee = services.employees.get(member.employee_id)
        obj = services.objects.get(member.object_id)
        await state.clear()
        await state.update_data(
            object_id=obj.id, member_id=member.id, payment_date=today().isoformat(), comment=None
        )
        await state.set_state(ObjectPaymentForm.amount)
        await callback.answer()
        await callback.message.answer(
            f"Выплата / аванс: {employee.name}, объект «{obj.name}».\n"
            f"Введите выплаченную сумму в {employee.currency}. Для отмены — /cancel."
        )

    @router.message(ObjectPaymentForm.amount)
    async def payment_amount(message: Message, state: FSMContext):
        await state.update_data(amount=str(money(message.text)))
        await show_payment_preview(message, state)

    @router.callback_query(ObjectPaymentForm.confirm, F.data == "payment:date")
    async def payment_change_date(callback: CallbackQuery, state: FSMContext):
        await state.set_state(ObjectPaymentForm.date)
        await callback.answer()
        await callback.message.answer("Дата выплаты:", reply_markup=kb.dates("paydate"))

    @router.callback_query(ObjectPaymentForm.confirm, F.data == "payment:comment")
    async def payment_change_comment(callback: CallbackQuery, state: FSMContext):
        await state.set_state(ObjectPaymentForm.comment)
        await callback.answer()
        await callback.message.answer("Комментарий к выплате или авансу. «-» — убрать комментарий:")

    async def payment_date_selected(message, state, payment_date):
        if payment_date > today():
            raise DomainError("Нельзя записать выплату будущей датой.")
        await state.update_data(payment_date=payment_date.isoformat())
        await show_payment_preview(message, state)

    @router.callback_query(ObjectPaymentForm.date, F.data.startswith("paydate:"))
    async def payment_date(callback: CallbackQuery, state: FSMContext):
        choice = callback.data.split(":", 1)[1]
        if choice == "custom":
            await state.set_state(ObjectPaymentForm.custom_date)
            await callback.message.answer("Введите дату ДД.ММ.ГГГГ:")
        elif choice in {"today", "yesterday"}:
            await payment_date_selected(
                callback.message, state, today() - timedelta(days=choice == "yesterday")
            )
        else:
            raise DomainError("Неизвестная дата.")
        await callback.answer()

    @router.message(ObjectPaymentForm.custom_date)
    async def payment_custom_date(message: Message, state: FSMContext):
        await payment_date_selected(message, state, parse_date(message.text))

    @router.message(ObjectPaymentForm.comment)
    async def payment_preview(message: Message, state: FSMContext):
        await state.update_data(comment=optional(message.text, "Комментарий", 1000))
        await show_payment_preview(message, state)

    async def show_payment_preview(message, state):
        data = await state.get_data()
        comment = data.get("comment")
        member = team.get(data["member_id"])
        employee = services.employees.get(member.employee_id)
        obj = services.objects.get(member.object_id)
        token = str(uuid.uuid4())
        await state.update_data(token=token)
        await state.set_state(ObjectPaymentForm.confirm)
        await message.answer(
            f"Записать выплату?\n{employee.name}\nОбъект: {obj.name}\n"
            f"Сумма: {data['amount']} {employee.currency}\n"
            f"Дата: {date.fromisoformat(data['payment_date']):%d.%m.%Y}\n"
            f"Комментарий: {comment or '—'}",
            reply_markup=buttons(
                ("Записать выплату", f"payok:{token}"),
                ("Изменить дату", "payment:date"),
                ("Комментарий", "payment:comment"),
                ("Отмена", f"member:{member.id}"),
            ),
        )

    @router.callback_query(ObjectPaymentForm.confirm, F.data.startswith("payok:"))
    async def payment_save(callback: CallbackQuery, state: FSMContext):
        data = await state.get_data()
        if callback.data.split(":", 1)[1] != data["token"]:
            raise DomainError("Это подтверждение устарело. Проверьте текущую выплату.")
        member = team.get(data["member_id"])
        payment_date = date.fromisoformat(data["payment_date"])
        if payment_date > today():
            raise DomainError("Нельзя записать выплату будущей датой.")
        services.payments.create(
            employee_id=member.employee_id,
            object_id=member.object_id,
            amount=data["amount"],
            payment_date=payment_date,
            method="other",
            comment=data["comment"],
            actor=callback.from_user.id,
            idempotency_key=f"object-payment:{data['token']}",
        )
        await callback.answer("Выплата записана")
        await show_member(callback.message, state, member.id)

    @router.callback_query(F.data.regexp(r"^pays:[0-9a-f-]{36}:[0-9]{1,8}$"))
    async def payment_history(callback: CallbackQuery, state: FSMContext):
        _, member_id, offset = callback.data.split(":")
        member = team.get(member_id)
        employee = services.employees.get(member.employee_id)
        obj = services.objects.get(member.object_id)
        rows = services.payments.history(member.employee_id, member.object_id, offset=int(offset))
        await state.clear()
        await state.update_data(object_id=obj.id, member_id=member.id)
        actions = [
            (f"{row.payment_date:%d.%m.%Y} · {row.amount} {row.currency}", f"payvoid:{row.id}")
            for row in rows
        ]
        if int(offset):
            actions.append(("Предыдущие 20", f"pays:{member.id}:{max(0, int(offset) - 20)}"))
        if len(rows) == 20:
            actions.append(("Следующие 20", f"pays:{member.id}:{int(offset) + 20}"))
        actions.append(("← Карточка на объекте", f"member:{member.id}"))
        await callback.answer()
        await callback.message.answer(
            f"Выплаты: {employee.name}, объект «{obj.name}».\n"
            + ("Выберите запись для просмотра и отмены ошибки." if rows else "Записей нет."),
            reply_markup=buttons(*actions),
        )

    @router.callback_query(F.data.regexp(r"^payvoid:[0-9a-f-]{36}$"))
    async def payment_void_preview(callback: CallbackQuery, state: FSMContext):
        row = services.payments.get(callback.data.split(":", 1)[1])
        if row.object_id is None or row.voided_at:
            raise DomainError("Выплата без объекта или уже отменена.")
        member = next(
            (member for member, emp, _ in team.roster(row.object_id) if emp.id == row.employee_id),
            None,
        )
        if member is None:
            raise DomainError("Карточка сотрудника на объекте не найдена.")
        employee = services.employees.get(row.employee_id)
        obj = services.objects.get(row.object_id)
        token = str(uuid.uuid4())
        await state.clear()
        await state.update_data(
            object_id=obj.id, member_id=member.id, payment_id=row.id, token=token
        )
        await state.set_state(ObjectPaymentForm.void)
        await callback.answer()
        await callback.message.answer(
            f"{employee.name}\nОбъект: {obj.name}\nВыплата: {row.amount} {row.currency}\n"
            f"Дата: {row.payment_date:%d.%m.%Y}\nКомментарий: {row.comment or '—'}\n\n"
            "Отменить ошибочную запись? Остаток будет пересчитан.",
            reply_markup=buttons(
                ("Подтвердить отмену выплаты", f"payvoidok:{token}"),
                ("Назад", f"pays:{member.id}:0"),
            ),
        )

    @router.callback_query(ObjectPaymentForm.void, F.data.startswith("payvoidok:"))
    async def payment_void_save(callback: CallbackQuery, state: FSMContext):
        data = await state.get_data()
        if callback.data.split(":", 1)[1] != data["token"]:
            raise DomainError("Это подтверждение устарело. Выберите выплату заново.")
        services.payments.void(
            data["payment_id"], actor=callback.from_user.id, reason="Исправление выплаты владельцем"
        )
        await callback.answer("Запись выплаты отменена")
        await show_member(callback.message, state, data["member_id"])

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
        await show_member(message, state, data["member_id"])

    @router.callback_query(F.data.regexp(r"^shift:[0-9a-f-]{36}$"))
    async def shift_start(callback: CallbackQuery, state: FSMContext):
        obj = services.objects.get(callback.data.split(":", 1)[1])
        if obj.status != "active":
            raise DomainError("Объект закрыт для новых смен.")
        await state.clear()
        await state.update_data(object_id=obj.id, employee_ids=[], work_date=today().isoformat())
        await callback.answer()
        await shift_picker(callback.message, state)

    @router.callback_query(ShiftForm.people, F.data == "shift:date")
    async def change_shift_date(callback: CallbackQuery, state: FSMContext):
        await state.set_state(ShiftForm.date)
        await callback.answer()
        await callback.message.answer(
            "За какой день отметить смены?", reply_markup=kb.dates("shiftdate")
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
            for member, emp, count in team.roster(obj.id, active_only=True)
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
            ("Изменить дату", "shift:date"),
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
                work_date=(today() - timedelta(days=choice == "yesterday")).isoformat(),
                employee_ids=[],
            )
            await shift_picker(callback.message, state)
        await callback.answer()

    @router.message(ShiftForm.custom_date)
    async def shift_custom_date(message: Message, state: FSMContext):
        await state.update_data(work_date=parse_date(message.text).isoformat(), employee_ids=[])
        await shift_picker(message, state)

    @router.callback_query(ShiftForm.people, F.data.startswith("toggle:"))
    async def toggle(callback: CallbackQuery, state: FSMContext):
        data = await state.get_data()
        employee_id = callback.data.split(":", 1)[1]
        allowed = {emp.id for _, emp, _ in team.roster(data["object_id"], active_only=True)}
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
                ("← История", f"memberlog:{member.id}"),
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

    @router.callback_query(F.data.regexp(r"^reportobj:[0-9a-f-]{36}$"))
    @router.callback_query(F.data == "team:export")
    async def export_menu(callback: CallbackQuery, state: FSMContext):
        data = await state.get_data()
        obj = services.objects.get(
            callback.data.split(":", 1)[1]
            if callback.data.startswith("reportobj:")
            else data.get("object_id", "")
        )
        await state.clear()
        await state.update_data(object_id=obj.id)
        await callback.answer()
        await callback.message.answer(
            f"Отчёт · {obj.name}\nСмены, начисления, выплаты и остатки за всё время.",
            reply_markup=buttons(
                ("Скачать Excel", "teamxlsx"),
                ("Скачать CSV", "teamcsv"),
                ("← Объект", f"obj:{obj.id}"),
            ),
        )

    @router.callback_query(F.data.in_({"teamcsv", "teamxlsx"}))
    async def export(callback: CallbackQuery, state: FSMContext):
        data = await state.get_data()
        obj = services.objects.get(data.get("object_id", ""))
        rows = []
        for member, emp, count in team.roster(obj.id):
            summary = services.payroll.summary(emp.id, object_id=obj.id)
            rows.append(
                [
                    emp.name,
                    count,
                    "В составе" if member.active else "Убран из состава",
                    summary.currency,
                    summary.earned,
                    summary.paid,
                    "Не определён" if summary.unrated_shifts else summary.balance,
                    summary.unrated_shifts,
                ]
            )
        headers = [
            "Сотрудник",
            "Смены",
            "Состав",
            "Валюта",
            "Начислено по сменам со ставкой",
            "Выплачено",
            "Остаток (минус — аванс)",
            "Смен без ставки",
        ]
        kind = "csv" if callback.data == "teamcsv" else "xlsx"
        payload = table_csv(headers, rows) if kind == "csv" else table_xlsx(obj.name, headers, rows)
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
