from __future__ import annotations

import calendar
import uuid
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from decimal import Decimal
from zoneinfo import ZoneInfo

from aiogram import F, Router
from aiogram.filters import Command, MagicData
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import BufferedInputFile, CallbackQuery, Message
from aiogram.utils.deep_linking import create_start_link
from aiogram.utils.keyboard import InlineKeyboardBuilder

from app.bot import keyboards as kb
from app.models import EmployeeRate
from app.reports import summaries_csv, summaries_xlsx, table_csv, table_xlsx
from app.services import (
    AttendanceService,
    DomainError,
    EmployeeService,
    ObjectService,
    PaymentService,
    PayrollService,
    ReportService,
    as_money,
    day_coefficient,
    money,
)


@dataclass(frozen=True)
class Services:
    employees: EmployeeService
    objects: ObjectService
    attendance: AttendanceService
    payroll: PayrollService
    payments: PaymentService
    reports: ReportService


class NewEmployee(StatesGroup):
    name = State()
    rate = State()
    currency = State()
    start_date = State()
    custom_start_date = State()


class NewObject(StatesGroup):
    name = State()
    address = State()
    start_date = State()
    custom_start_date = State()


class EditEmployee(StatesGroup):
    value = State()


class EditObject(StatesGroup):
    value = State()


class AttendanceFlow(StatesGroup):
    date = State()
    custom_date = State()
    object = State()
    employees = State()
    coefficient = State()
    custom_coefficient = State()
    individual_coefficient = State()
    confirm = State()


class PaymentFlow(StatesGroup):
    employee = State()
    date = State()
    custom_date = State()
    object = State()
    amount = State()
    method = State()
    confirm = State()


class RateFlow(StatesGroup):
    amount = State()
    date = State()
    custom_date = State()


class VoidFlow(StatesGroup):
    reason = State()


class ReportFlow(StatesGroup):
    period = State()
    custom_from = State()
    custom_to = State()
    ready = State()


def parse_date(value: str) -> date:
    try:
        day, month, year = (int(part) for part in (value or "").strip().split("."))
        return date(year, month, day)
    except (TypeError, ValueError) as exc:
        raise DomainError("Введите дату в формате ДД.ММ.ГГГГ.") from exc


def month_bounds(today: date) -> tuple[date, date]:
    return today.replace(day=1), today.replace(day=calendar.monthrange(today.year, today.month)[1])


def validate_report_period(start_date: date, end_date: date) -> None:
    if end_date < start_date:
        raise DomainError("Конец периода не может быть раньше начала.")
    if (end_date - start_date).days > 731:
        raise DomainError("Максимальный период отчёта — 2 года.")


def amount(value: Decimal, currency: str) -> str:
    return f"{as_money(value)} {currency}"


def totals_by_currency(summaries) -> dict[str, tuple[Decimal, Decimal]]:
    totals: dict[str, tuple[Decimal, Decimal]] = {}
    for item in summaries:
        earned, paid = totals.get(item.currency, (Decimal(0), Decimal(0)))
        totals[item.currency] = earned + item.earned, paid + item.paid
    return totals


async def answer_long(message: Message, text: str, *, reply_markup=None) -> None:
    chunks: list[str] = []
    current = ""
    for line in text.splitlines() or [""]:
        candidate = f"{current}\n{line}" if current else line
        if len(candidate) <= 3900:
            current = candidate
            continue
        if current:
            chunks.append(current)
        while len(line) > 3900:
            chunks.append(line[:3900])
            line = line[3900:]
        current = line
    if current or not chunks:
        chunks.append(current)
    for index, chunk in enumerate(chunks):
        await message.answer(chunk, reply_markup=reply_markup if index == len(chunks) - 1 else None)


def build_router(services: Services, *, timezone_name: str, default_currency: str) -> Router:
    router = Router(name="payroll")
    router.message.filter(MagicData(F.is_owner))
    router.callback_query.filter(MagicData(F.is_owner))
    timezone = ZoneInfo(timezone_name)

    def today() -> date:
        return datetime.now(timezone).date()

    async def show_main(message: Message) -> None:
        start, end = month_bounds(today())
        summaries = services.payroll.all_summaries(date_from=start, date_to=end)
        debts = services.payroll.all_summaries(date_to=today())
        lines = [
            "\n".join(
                (
                    f"Сегодня, {today().strftime('%d.%m.%Y')}",
                    "",
                    f"🏗 Активных объектов: {len(services.objects.list())}",
                    f"👷 Активных сотрудников: {len(services.employees.list())}",
                )
            )
        ]
        period_totals = totals_by_currency(summaries)
        debt_totals = {
            currency: earned - paid
            for currency, (earned, paid) in totals_by_currency(debts).items()
        }
        if not period_totals:
            period_totals[default_currency] = Decimal(0), Decimal(0)
        for currency, (earned, paid) in sorted(period_totals.items()):
            lines.append(
                f"\n💰 Начислено: {amount(earned, currency)}\n"
                f"💵 Выплачено: {amount(paid, currency)}\n"
                f"⚠️ Общий долг: {amount(debt_totals.get(currency, Decimal(0)), currency)}"
            )
        await message.answer("\n".join(lines), reply_markup=kb.MAIN)

    async def show_objects(message: Message) -> None:
        rows = services.objects.list()
        text = "🏗 Объекты" if rows else "Объектов пока нет."
        await message.answer(
            text, reply_markup=kb.entity_list(rows, "obj", create_callback="obj:create")
        )

    async def show_employees(message: Message, prefix: str = "emp") -> None:
        rows = services.employees.list()
        text = "👷 Сотрудники" if rows else "Сотрудников пока нет."
        await message.answer(
            text,
            reply_markup=kb.entity_list(
                rows, prefix, create_callback="emp:create" if prefix == "emp" else None
            ),
        )

    async def attendance_choose_object(target: Message, state: FSMContext) -> None:
        rows = services.objects.list()
        if not rows:
            await target.answer("Сначала создайте объект.")
            await state.clear()
            return
        await state.set_state(AttendanceFlow.object)
        await target.answer("Выберите объект:", reply_markup=kb.entity_list(rows, "att:obj"))

    async def attendance_choose_people(target: Message, state: FSMContext) -> None:
        data = await state.get_data()
        selected = set(data.get("employee_ids", []))
        if selected:
            await state.set_state(AttendanceFlow.coefficient)
            await target.answer("Выберите коэффициент дня:", reply_markup=kb.COEFFICIENTS)
            return
        employees = services.employees.list()
        if not employees:
            await target.answer("Сначала создайте сотрудников.")
            await state.clear()
            return
        await state.set_state(AttendanceFlow.employees)
        await target.answer(
            "Кто работал? Нажмите на всех нужных сотрудников:",
            reply_markup=kb.employee_picker(employees, selected),
        )

    async def attendance_preview(target: Message, state: FSMContext) -> None:
        data = await state.get_data()
        rows = services.attendance.preview(
            employee_ids=data["employee_ids"],
            object_id=data["object_id"],
            work_date=date.fromisoformat(data["work_date"]),
            coefficient=data["coefficient"],
        )
        obj = services.objects.get(data["object_id"])
        lines = [date.fromisoformat(data["work_date"]).strftime("%d.%m.%Y"), obj.name, ""]
        totals: dict[str, Decimal] = {}
        for row in rows:
            lines.append(
                f"{row.employee_name} — {row.coefficient} × {row.rate} = "
                f"{amount(row.earned, row.currency)}"
            )
            totals[row.currency] = totals.get(row.currency, Decimal(0)) + row.earned
        lines.append("")
        lines.extend(
            f"Всего начислено: {amount(total, currency)}"
            for currency, total in sorted(totals.items())
        )
        await state.set_state(AttendanceFlow.confirm)
        await answer_long(target, "\n".join(lines), reply_markup=kb.CONFIRM_ATTENDANCE)

    async def payment_for(target: Message, state: FSMContext, employee_id: str) -> None:
        summary = services.payroll.summary(employee_id)
        await state.set_state(PaymentFlow.date)
        await state.update_data(
            employee_id=employee_id, operation_key=f"telegram:payment:{uuid.uuid4()}"
        )
        await target.answer(
            f"{summary.employee_name}\nНачислено: {amount(summary.earned, summary.currency)}\n"
            f"Выплачено: {amount(summary.paid, summary.currency)}\n"
            f"Остаток: {amount(summary.balance, summary.currency)}\n\nВыберите дату выплаты:",
            reply_markup=kb.dates("pay:date"),
        )

    @router.message(Command("start", "menu"))
    async def start(message: Message, state: FSMContext):
        await state.clear()
        await show_main(message)

    @router.callback_query(F.data == "cancel")
    async def cancel(callback: CallbackQuery, state: FSMContext):
        await state.clear()
        await callback.answer("Отменено")
        if callback.message:
            await callback.message.answer("Действие отменено.", reply_markup=kb.MAIN)

    @router.message(F.text == "🏗 Объекты")
    async def objects_menu(message: Message, state: FSMContext):
        await state.clear()
        await show_objects(message)

    @router.callback_query(F.data == "obj:create")
    async def object_create(callback: CallbackQuery, state: FSMContext):
        await state.set_state(NewObject.name)
        await callback.answer()
        await callback.message.answer("Название объекта:")

    @router.message(NewObject.name)
    async def object_name(message: Message, state: FSMContext):
        await state.update_data(name=message.text)
        await state.set_state(NewObject.address)
        await message.answer("Адрес объекта или «-»:")

    @router.message(NewObject.address)
    async def object_address(message: Message, state: FSMContext):
        await state.update_data(
            address=None if message.text.strip() == "-" else message.text.strip()
        )
        await state.set_state(NewObject.start_date)
        await message.answer("Дата начала объекта:", reply_markup=kb.dates("objstart"))

    async def save_new_object(
        target: Message, state: FSMContext, start_date: date, actor: int
    ) -> None:
        data = await state.get_data()
        try:
            row = services.objects.create(
                name=data["name"],
                address=data["address"],
                start_date=start_date,
                actor=actor,
            )
        except DomainError as exc:
            await target.answer(str(exc))
            return
        await state.clear()
        await target.answer(f"Объект «{row.name}» создан.", reply_markup=kb.MAIN)

    @router.callback_query(NewObject.start_date, F.data.startswith("objstart:"))
    async def object_start_date(callback: CallbackQuery, state: FSMContext):
        choice = callback.data.rsplit(":", 1)[1]
        if choice == "custom":
            await state.set_state(NewObject.custom_start_date)
            await callback.message.answer("Введите дату ДД.ММ.ГГГГ:")
        else:
            selected = today() if choice == "today" else today() - timedelta(days=1)
            await save_new_object(callback.message, state, selected, callback.from_user.id)
        await callback.answer()

    @router.message(NewObject.custom_start_date)
    async def object_custom_start_date(message: Message, state: FSMContext):
        try:
            selected = parse_date(message.text)
        except DomainError as exc:
            await message.answer(str(exc))
            return
        await save_new_object(message, state, selected, message.from_user.id)

    @router.callback_query(F.data.startswith("obj:"))
    async def object_card(callback: CallbackQuery):
        if callback.data == "obj:create":
            return
        row = services.objects.get(callback.data.split(":", 1)[1])
        await callback.answer()
        builder = InlineKeyboardBuilder()
        builder.button(text="Изменить название", callback_data=f"objfield:name:{row.id}")
        builder.button(text="Изменить адрес", callback_data=f"objfield:address:{row.id}")
        builder.button(text="Описание", callback_data=f"objfield:description:{row.id}")
        builder.button(text="Комментарий", callback_data=f"objfield:comment:{row.id}")
        for status, label in (
            ("active", "Активен"),
            ("completed", "Завершён"),
            ("archived", "Архив"),
        ):
            builder.button(text=label, callback_data=f"objstatus:{status}:{row.id}")
        builder.adjust(2, 2, 3)
        await callback.message.answer(
            f"🏗 {row.name}\nАдрес: {row.address or '—'}\nСтатус: {row.status}\n"
            f"Начало: {row.start_date.strftime('%d.%m.%Y')}",
            reply_markup=builder.as_markup(),
        )

    @router.callback_query(F.data.startswith("objfield:"))
    async def object_edit_start(callback: CallbackQuery, state: FSMContext):
        _, field, object_id = callback.data.split(":", 2)
        await state.set_state(EditObject.value)
        await state.update_data(edit_object_id=object_id, edit_field=field)
        await callback.answer()
        await callback.message.answer("Введите новое значение. «-» очищает поле:")

    @router.message(EditObject.value)
    async def object_edit_value(message: Message, state: FSMContext):
        data = await state.get_data()
        row = services.objects.get(data["edit_object_id"])
        value = None if message.text.strip() == "-" else message.text.strip()
        values = {
            "name": row.name,
            "address": row.address,
            "description": row.description,
            "comment": row.comment,
        }
        values[data["edit_field"]] = value
        try:
            services.objects.update(row.id, actor=message.from_user.id, **values)
        except DomainError as exc:
            await message.answer(str(exc))
            return
        await state.clear()
        await message.answer("Объект обновлён.", reply_markup=kb.MAIN)

    @router.callback_query(F.data.startswith("objstatus:"))
    async def object_status(callback: CallbackQuery):
        _, status, object_id = callback.data.split(":", 2)
        services.objects.set_status(object_id, status, actor=callback.from_user.id)
        await callback.answer("Статус изменён")
        await callback.message.answer(f"Статус объекта: {status}.")

    @router.message(F.text == "👷 Сотрудники")
    async def employees_menu(message: Message, state: FSMContext):
        await state.clear()
        await show_employees(message)

    @router.callback_query(F.data == "emp:create")
    async def employee_create(callback: CallbackQuery, state: FSMContext):
        await state.set_state(NewEmployee.name)
        await callback.answer()
        await callback.message.answer("Имя сотрудника:")

    @router.message(NewEmployee.name)
    async def employee_name(message: Message, state: FSMContext):
        await state.update_data(name=message.text)
        await state.set_state(NewEmployee.rate)
        await message.answer(f"Дневная ставка в {default_currency}:")

    @router.message(NewEmployee.rate)
    async def employee_rate(message: Message, state: FSMContext):
        try:
            parsed_rate = money(message.text)
        except DomainError as exc:
            await message.answer(str(exc))
            return
        await state.update_data(rate=str(parsed_rate))
        await state.set_state(NewEmployee.currency)
        await message.answer(f"Валюта: RUB, EUR, USD… Введите код или «-» для {default_currency}:")

    @router.message(NewEmployee.currency)
    async def employee_currency(message: Message, state: FSMContext):
        currency = default_currency if message.text.strip() == "-" else message.text.strip().upper()
        if len(currency) != 3 or not currency.isascii() or not currency.isalpha():
            await message.answer("Введите трёхбуквенный код валюты, например RUB или EUR.")
            return
        await state.update_data(currency=currency)
        await state.set_state(NewEmployee.start_date)
        await message.answer("Дата начала работы:", reply_markup=kb.dates("empstart"))

    async def save_new_employee(
        target: Message, state: FSMContext, start_date: date, actor: int
    ) -> None:
        data = await state.get_data()
        try:
            row = services.employees.create(
                name=data["name"],
                rate=data["rate"],
                currency=data["currency"],
                start_date=start_date,
                actor=actor,
            )
        except DomainError as exc:
            await target.answer(str(exc))
            return
        await state.clear()
        await target.answer(f"Сотрудник {row.name} создан.", reply_markup=kb.MAIN)

    @router.callback_query(NewEmployee.start_date, F.data.startswith("empstart:"))
    async def employee_start_date(callback: CallbackQuery, state: FSMContext):
        choice = callback.data.rsplit(":", 1)[1]
        if choice == "custom":
            await state.set_state(NewEmployee.custom_start_date)
            await callback.message.answer("Введите дату ДД.ММ.ГГГГ:")
        else:
            selected = today() if choice == "today" else today() - timedelta(days=1)
            await save_new_employee(callback.message, state, selected, callback.from_user.id)
        await callback.answer()

    @router.message(NewEmployee.custom_start_date)
    async def employee_custom_start_date(message: Message, state: FSMContext):
        try:
            selected = parse_date(message.text)
        except DomainError as exc:
            await message.answer(str(exc))
            return
        await save_new_employee(message, state, selected, message.from_user.id)

    @router.callback_query(F.data.startswith("emp:"))
    async def employee_card(callback: CallbackQuery):
        if callback.data == "emp:create":
            return
        employee_id = callback.data.split(":", 1)[1]
        employee = services.employees.get(employee_id)
        current_rate: EmployeeRate = services.employees.effective_rate(employee_id, today())
        month_start, month_end = month_bounds(today())
        current = services.payroll.summary(employee_id, date_from=month_start, date_to=month_end)
        lifetime = services.payroll.summary(employee_id)
        builder = InlineKeyboardBuilder()
        builder.button(text="📅 Добавить день", callback_data=f"empday:{employee_id}")
        builder.button(text="💵 Выплатить", callback_data=f"emppay:{employee_id}")
        builder.button(text="📜 История", callback_data=f"history:{employee_id}")
        builder.button(text="💰 Изменить ставку", callback_data=f"rate:{employee_id}")
        builder.button(text="✏️ Изменить", callback_data=f"empedit:{employee_id}")
        builder.button(
            text="Деактивировать" if employee.status == "active" else "Активировать",
            callback_data=f"empstatus:{employee_id}",
        )
        if employee.telegram_id is None and employee.status == "active":
            builder.button(
                text="🔗 Пригласить сотрудника", callback_data=f"empinvite:{employee_id}"
            )
        builder.adjust(2, 2, 2)
        await callback.answer()
        await callback.message.answer(
            f"👷 {employee.name}\nСтавка: {amount(current_rate.daily_rate, employee.currency)} / день\n\n"
            f"Текущий месяц:\nДней: {current.days}\nНачислено: {amount(current.earned, current.currency)}\n"
            f"Выплачено: {amount(current.paid, current.currency)}\nОстаток: {amount(current.balance, current.currency)}\n\n"
            f"Всего:\nНачислено: {amount(lifetime.earned, lifetime.currency)}\n"
            f"Выплачено: {amount(lifetime.paid, lifetime.currency)}\n"
            f"Остаток: {amount(lifetime.balance, lifetime.currency)}\n\n"
            f"Telegram ID: {employee.telegram_id or 'не привязан'}\n"
            f"Реквизиты:\n{employee.payment_details or 'не заполнены'}",
            reply_markup=builder.as_markup(),
        )

    @router.callback_query(F.data.startswith("empinvite:"))
    async def employee_invite(callback: CallbackQuery):
        try:
            token = services.employees.create_invite(
                callback.data.split(":", 1)[1], actor=callback.from_user.id
            )
        except DomainError as exc:
            await callback.answer(str(exc), show_alert=True)
            return
        link = await create_start_link(callback.bot, f"employee_{token}")
        await callback.answer()
        await callback.message.answer(
            f"Отправьте эту ссылку лично сотруднику:\n{link}\n\n"
            "Он сможет заполнить ФИО и реквизиты в своей карточке. "
            "Ссылка одноразовая, действует 7 дней. Создание новой ссылки отменяет предыдущую."
        )

    @router.callback_query(F.data.startswith("empedit:"))
    async def employee_edit(callback: CallbackQuery):
        employee_id = callback.data.split(":", 1)[1]
        builder = InlineKeyboardBuilder()
        for field, label in (
            ("name", "Имя"),
            ("phone", "Телефон"),
            ("telegram_id", "Telegram ID"),
            ("comment", "Комментарий"),
        ):
            builder.button(text=label, callback_data=f"empfield:{field}:{employee_id}")
        builder.adjust(2)
        await callback.answer()
        await callback.message.answer("Что изменить?", reply_markup=builder.as_markup())

    @router.callback_query(F.data.startswith("empfield:"))
    async def employee_edit_start(callback: CallbackQuery, state: FSMContext):
        _, field, employee_id = callback.data.split(":", 2)
        await state.set_state(EditEmployee.value)
        await state.update_data(edit_employee_id=employee_id, edit_field=field)
        await callback.answer()
        await callback.message.answer("Введите новое значение. «-» очищает поле:")

    @router.message(EditEmployee.value)
    async def employee_edit_value(message: Message, state: FSMContext):
        data = await state.get_data()
        employee = services.employees.get(data["edit_employee_id"])
        raw = message.text.strip()
        field = data["edit_field"]
        values = {
            "name": employee.name,
            "phone": employee.phone,
            "telegram_id": employee.telegram_id,
            "comment": employee.comment,
        }
        try:
            if field == "telegram_id":
                values[field] = None if raw == "-" else int(raw)
            else:
                values[field] = None if raw == "-" else raw
            services.employees.update(employee.id, actor=message.from_user.id, **values)
        except (DomainError, ValueError) as exc:
            await message.answer(str(exc) if str(exc) else "Некорректное значение.")
            return
        await state.clear()
        await message.answer("Сотрудник обновлён.", reply_markup=kb.MAIN)

    @router.callback_query(F.data.startswith("empstatus:"))
    async def employee_status(callback: CallbackQuery):
        employee_id = callback.data.split(":", 1)[1]
        employee = services.employees.get(employee_id)
        status = "inactive" if employee.status == "active" else "active"
        services.employees.set_status(employee_id, status, actor=callback.from_user.id)
        await callback.answer("Статус изменён")
        await callback.message.answer(f"Статус сотрудника: {status}.")

    @router.callback_query(F.data.regexp(r"^rate:[0-9a-f-]{36}$"))
    async def rate_start(callback: CallbackQuery, state: FSMContext):
        await state.set_state(RateFlow.amount)
        await state.update_data(employee_id=callback.data.split(":", 1)[1])
        await callback.answer()
        await callback.message.answer("Новая дневная ставка:")

    @router.message(RateFlow.amount)
    async def rate_amount(message: Message, state: FSMContext):
        try:
            parsed_rate = money(message.text)
        except DomainError as exc:
            await message.answer(str(exc))
            return
        await state.update_data(rate=str(parsed_rate))
        await state.set_state(RateFlow.date)
        await message.answer("С какой даты действует ставка?", reply_markup=kb.dates("rate:date"))

    @router.callback_query(RateFlow.date, F.data.startswith("rate:date:"))
    async def rate_date(callback: CallbackQuery, state: FSMContext):
        choice = callback.data.rsplit(":", 1)[1]
        if choice == "custom":
            await state.set_state(RateFlow.custom_date)
            await callback.message.answer("Введите дату ДД.ММ.ГГГГ:")
            await callback.answer()
            return
        effective = today() if choice == "today" else today() - timedelta(days=1)
        data = await state.get_data()
        try:
            services.employees.change_rate(
                data["employee_id"], data["rate"], effective, callback.from_user.id
            )
        except DomainError as exc:
            await callback.message.answer(str(exc))
            return
        await state.clear()
        await callback.answer("Сохранено")
        await callback.message.answer("Новая ставка сохранена. Старые начисления не изменены.")

    @router.message(RateFlow.custom_date)
    async def rate_custom_date(message: Message, state: FSMContext):
        data = await state.get_data()
        try:
            effective = parse_date(message.text)
            services.employees.change_rate(
                data["employee_id"], data["rate"], effective, message.from_user.id
            )
        except DomainError as exc:
            await message.answer(str(exc))
            return
        await state.clear()
        await message.answer("Новая ставка сохранена. Старые начисления не изменены.")

    @router.message(F.text == "📅 Рабочие дни")
    async def attendance_start(message: Message, state: FSMContext):
        await state.clear()
        await state.update_data(
            operation_key=f"telegram:attendance:{uuid.uuid4()}", employee_ids=[]
        )
        await state.set_state(AttendanceFlow.date)
        await message.answer("Выберите дату:", reply_markup=kb.dates("att:date"))

    @router.callback_query(F.data.startswith("empday:"))
    async def employee_day(callback: CallbackQuery, state: FSMContext):
        await state.clear()
        await state.update_data(
            operation_key=f"telegram:attendance:{uuid.uuid4()}",
            employee_ids=[callback.data.split(":", 1)[1]],
        )
        await state.set_state(AttendanceFlow.date)
        await callback.answer()
        await callback.message.answer("Выберите дату:", reply_markup=kb.dates("att:date"))

    @router.callback_query(AttendanceFlow.date, F.data.startswith("att:date:"))
    async def attendance_date(callback: CallbackQuery, state: FSMContext):
        choice = callback.data.rsplit(":", 1)[1]
        if choice == "custom":
            await state.set_state(AttendanceFlow.custom_date)
            await callback.message.answer("Введите дату ДД.ММ.ГГГГ:")
        else:
            selected_date = today() if choice == "today" else today() - timedelta(days=1)
            await state.update_data(work_date=selected_date.isoformat())
            await attendance_choose_object(callback.message, state)
        await callback.answer()

    @router.message(AttendanceFlow.custom_date)
    async def attendance_custom_date(message: Message, state: FSMContext):
        try:
            selected_date = parse_date(message.text)
        except DomainError as exc:
            await message.answer(str(exc))
            return
        await state.update_data(work_date=selected_date.isoformat())
        await attendance_choose_object(message, state)

    @router.callback_query(AttendanceFlow.object, F.data.startswith("att:obj:"))
    async def attendance_object(callback: CallbackQuery, state: FSMContext):
        await state.update_data(object_id=callback.data.rsplit(":", 1)[1])
        await callback.answer()
        await attendance_choose_people(callback.message, state)

    @router.callback_query(AttendanceFlow.employees, F.data.startswith("att:toggle:"))
    async def attendance_toggle(callback: CallbackQuery, state: FSMContext):
        employee_id = callback.data.rsplit(":", 1)[1]
        data = await state.get_data()
        selected = set(data.get("employee_ids", []))
        selected.symmetric_difference_update({employee_id})
        await state.update_data(employee_ids=list(selected))
        await callback.answer()
        await callback.message.edit_reply_markup(
            reply_markup=kb.employee_picker(services.employees.list(), selected)
        )

    @router.callback_query(AttendanceFlow.employees, F.data == "att:selected")
    async def attendance_selected(callback: CallbackQuery, state: FSMContext):
        data = await state.get_data()
        if not data.get("employee_ids"):
            await callback.answer("Выберите сотрудника", show_alert=True)
            return
        await state.set_state(AttendanceFlow.coefficient)
        await callback.answer()
        await callback.message.answer("Выберите коэффициент дня:", reply_markup=kb.COEFFICIENTS)

    @router.callback_query(AttendanceFlow.coefficient, F.data.startswith("att:coef:"))
    async def attendance_coefficient(callback: CallbackQuery, state: FSMContext):
        value = callback.data.rsplit(":", 1)[1]
        if value == "custom":
            await state.set_state(AttendanceFlow.custom_coefficient)
            await callback.message.answer("Введите коэффициент, например 0.75 или 1.5:")
        elif value == "individual":
            data = await state.get_data()
            employee_ids = data["employee_ids"]
            await state.update_data(individual_index=0, individual_coefficients={})
            await state.set_state(AttendanceFlow.individual_coefficient)
            employee = services.employees.get(employee_ids[0])
            await callback.message.answer(f"Коэффициент для {employee.name}:")
        else:
            await state.update_data(coefficient=value)
            try:
                await attendance_preview(callback.message, state)
            except DomainError as exc:
                await callback.message.answer(str(exc))
        await callback.answer()

    @router.message(AttendanceFlow.custom_coefficient)
    async def attendance_custom_coefficient(message: Message, state: FSMContext):
        await state.update_data(coefficient=message.text)
        try:
            await attendance_preview(message, state)
        except DomainError as exc:
            await message.answer(str(exc))

    @router.message(AttendanceFlow.individual_coefficient)
    async def attendance_individual_coefficient(message: Message, state: FSMContext):
        try:
            value = day_coefficient(message.text)
        except DomainError as exc:
            await message.answer(str(exc))
            return
        data = await state.get_data()
        employee_ids = data["employee_ids"]
        index = int(data["individual_index"])
        coefficients = dict(data["individual_coefficients"])
        coefficients[employee_ids[index]] = str(value)
        index += 1
        await state.update_data(
            individual_index=index,
            individual_coefficients=coefficients,
        )
        if index < len(employee_ids):
            employee = services.employees.get(employee_ids[index])
            await message.answer(f"Коэффициент для {employee.name}:")
            return
        await state.update_data(coefficient=coefficients)
        await attendance_preview(message, state)

    @router.callback_query(AttendanceFlow.confirm, F.data == "att:change")
    async def attendance_change(callback: CallbackQuery, state: FSMContext):
        await state.set_state(AttendanceFlow.coefficient)
        await callback.answer()
        await callback.message.answer("Выберите коэффициент:", reply_markup=kb.COEFFICIENTS)

    @router.callback_query(AttendanceFlow.confirm, F.data == "att:save")
    async def attendance_save(callback: CallbackQuery, state: FSMContext):
        data = await state.get_data()
        try:
            rows = services.attendance.create_bulk(
                employee_ids=data["employee_ids"],
                object_id=data["object_id"],
                work_date=date.fromisoformat(data["work_date"]),
                coefficient=data["coefficient"],
                actor=callback.from_user.id,
                operation_key=data["operation_key"],
            )
        except DomainError as exc:
            await callback.answer(str(exc), show_alert=True)
            return
        await state.clear()
        await callback.answer("Сохранено")
        await callback.message.answer(f"Сохранено рабочих дней: {len(rows)}.", reply_markup=kb.MAIN)

    @router.message(F.text == "💵 Выплаты")
    async def payment_menu(message: Message, state: FSMContext):
        await state.clear()
        await state.set_state(PaymentFlow.employee)
        await show_employees(message, "payemp")

    @router.callback_query(PaymentFlow.employee, F.data.startswith("payemp:"))
    async def payment_employee(callback: CallbackQuery, state: FSMContext):
        await callback.answer()
        await payment_for(callback.message, state, callback.data.split(":", 1)[1])

    @router.callback_query(F.data.startswith("emppay:"))
    async def employee_payment(callback: CallbackQuery, state: FSMContext):
        await callback.answer()
        await payment_for(callback.message, state, callback.data.split(":", 1)[1])

    async def payment_choose_object(target: Message, state: FSMContext) -> None:
        builder = InlineKeyboardBuilder()
        builder.button(text="Без привязки к объекту", callback_data="pay:obj:none")
        for obj in services.objects.list():
            builder.button(text=obj.name, callback_data=f"pay:obj:{obj.id}")
        builder.button(text="Отмена", callback_data="cancel")
        builder.adjust(1)
        await state.set_state(PaymentFlow.object)
        await target.answer("Привязать выплату к объекту?", reply_markup=builder.as_markup())

    @router.callback_query(PaymentFlow.date, F.data.startswith("pay:date:"))
    async def payment_date(callback: CallbackQuery, state: FSMContext):
        choice = callback.data.rsplit(":", 1)[1]
        if choice == "custom":
            await state.set_state(PaymentFlow.custom_date)
            await callback.message.answer("Введите дату ДД.ММ.ГГГГ:")
        else:
            selected_date = today() if choice == "today" else today() - timedelta(days=1)
            await state.update_data(payment_date=selected_date.isoformat())
            await payment_choose_object(callback.message, state)
        await callback.answer()

    @router.message(PaymentFlow.custom_date)
    async def payment_custom_date(message: Message, state: FSMContext):
        try:
            selected_date = parse_date(message.text)
        except DomainError as exc:
            await message.answer(str(exc))
            return
        await state.update_data(payment_date=selected_date.isoformat())
        await payment_choose_object(message, state)

    @router.callback_query(PaymentFlow.object, F.data.startswith("pay:obj:"))
    async def payment_object(callback: CallbackQuery, state: FSMContext):
        value = callback.data.rsplit(":", 1)[1]
        await state.update_data(object_id=None if value == "none" else value)
        await state.set_state(PaymentFlow.amount)
        await callback.answer()
        await callback.message.answer("Введите сумму выплаты:")

    @router.message(PaymentFlow.amount)
    async def payment_amount(message: Message, state: FSMContext):
        try:
            parsed_amount = money(message.text)
        except DomainError as exc:
            await message.answer(str(exc))
            return
        await state.update_data(amount=str(parsed_amount))
        await state.set_state(PaymentFlow.method)
        await message.answer("Способ выплаты:", reply_markup=kb.PAYMENT_METHODS)

    @router.callback_query(PaymentFlow.method, F.data.startswith("pay:method:"))
    async def payment_method(callback: CallbackQuery, state: FSMContext):
        method = callback.data.rsplit(":", 1)[1]
        data = await state.get_data()
        try:
            parsed_amount = money(data["amount"])
            summary = services.payroll.summary(data["employee_id"])
        except DomainError as exc:
            await callback.answer(str(exc), show_alert=True)
            return
        await state.update_data(method=method)
        await state.set_state(PaymentFlow.confirm)
        object_name = (
            services.objects.get(data["object_id"]).name if data.get("object_id") else "без объекта"
        )
        await callback.answer()
        await callback.message.answer(
            f"Выплата: {summary.employee_name}\n{amount(parsed_amount, summary.currency)}\n"
            f"{date.fromisoformat(data['payment_date']).strftime('%d.%m.%Y')}\n\n"
            f"Объект: {object_name}\n"
            "После выплаты останется: "
            f"{amount(summary.balance - parsed_amount, summary.currency)}",
            reply_markup=kb.CONFIRM_PAYMENT,
        )

    @router.callback_query(PaymentFlow.confirm, F.data == "pay:save")
    async def payment_save(callback: CallbackQuery, state: FSMContext):
        data = await state.get_data()
        try:
            row = services.payments.create(
                employee_id=data["employee_id"],
                amount=data["amount"],
                payment_date=date.fromisoformat(data["payment_date"]),
                method=data["method"],
                actor=callback.from_user.id,
                idempotency_key=data["operation_key"],
                object_id=data.get("object_id"),
            )
        except DomainError as exc:
            await callback.answer(str(exc), show_alert=True)
            return
        await state.clear()
        await callback.answer("Сохранено")
        await callback.message.answer(
            f"Выплата {amount(row.amount, row.currency)} сохранена.", reply_markup=kb.MAIN
        )

    @router.message(F.text == "💰 Зарплаты")
    async def salaries(message: Message, state: FSMContext):
        await state.clear()
        summaries = services.payroll.all_summaries()
        if not summaries:
            await message.answer("Сотрудников пока нет.")
            return
        lines = ["💰 Зарплаты"]
        for row in summaries:
            lines.append(f"{row.employee_name}: {amount(row.balance, row.currency)}")
        await answer_long(message, "\n".join(lines))

    @router.callback_query(F.data.startswith("history:"))
    async def history(callback: CallbackQuery):
        employee_id = callback.data.split(":", 1)[1]
        start_date, end_date = month_bounds(today())
        attendance, payments = services.reports.employee_history(employee_id, start_date, end_date)
        employee = services.employees.get(employee_id)
        lines = [f"📜 {employee.name}, текущий месяц", ""]
        lines.extend(
            f"{row.work_date.strftime('%d.%m')} — {row.coefficient} дня, {amount(row.earned_amount, row.currency)}"
            for row in attendance
        )
        lines.extend(
            f"Выплата {row.payment_date.strftime('%d.%m')} — {amount(row.amount, row.currency)}"
            for row in payments
        )
        builder = InlineKeyboardBuilder()
        for row in attendance:
            builder.button(
                text=f"Отменить день {row.work_date:%d.%m}", callback_data=f"voidatt:{row.id}"
            )
        for row in payments:
            builder.button(
                text=f"Отменить выплату {row.payment_date:%d.%m}",
                callback_data=f"voidpay:{row.id}",
            )
        builder.adjust(1)
        await callback.answer()
        await answer_long(
            callback.message,
            "\n".join(lines) if len(lines) > 2 else "История пуста.",
            reply_markup=builder.as_markup() if attendance or payments else None,
        )

    @router.callback_query(F.data.startswith("voidatt:"))
    async def void_attendance_preview(callback: CallbackQuery):
        row = services.attendance.get(callback.data.split(":", 1)[1])
        builder = InlineKeyboardBuilder()
        builder.button(text="Подтвердить отмену", callback_data=f"void:att:{row.id}")
        builder.button(text="Не отменять", callback_data="cancel")
        builder.adjust(1)
        await callback.answer()
        await callback.message.answer(
            f"Рабочий день {row.work_date:%d.%m.%Y}: {row.coefficient} дня, "
            f"{amount(row.earned_amount, row.currency)}.\n"
            f"После отмены долг уменьшится на {amount(row.earned_amount, row.currency)}.\n"
            "Запись останется в audit trail.",
            reply_markup=builder.as_markup(),
        )

    @router.callback_query(F.data.startswith("voidpay:"))
    async def void_payment_preview(callback: CallbackQuery):
        row = services.payments.get(callback.data.split(":", 1)[1])
        builder = InlineKeyboardBuilder()
        builder.button(text="Подтвердить отмену", callback_data=f"void:pay:{row.id}")
        builder.button(text="Не отменять", callback_data="cancel")
        builder.adjust(1)
        await callback.answer()
        await callback.message.answer(
            f"Выплата {row.payment_date:%d.%m.%Y}: {amount(row.amount, row.currency)}.\n"
            f"После отмены долг увеличится на {amount(row.amount, row.currency)}.\n"
            "Запись останется в audit trail.",
            reply_markup=builder.as_markup(),
        )

    @router.callback_query(F.data.startswith("void:att:"))
    async def void_attendance_confirm(callback: CallbackQuery, state: FSMContext):
        await state.set_state(VoidFlow.reason)
        await state.update_data(void_type="attendance", void_id=callback.data.rsplit(":", 1)[1])
        await callback.answer()
        await callback.message.answer("Укажите причину отмены:")

    @router.callback_query(F.data.startswith("void:pay:"))
    async def void_payment_confirm(callback: CallbackQuery, state: FSMContext):
        await state.set_state(VoidFlow.reason)
        await state.update_data(void_type="payment", void_id=callback.data.rsplit(":", 1)[1])
        await callback.answer()
        await callback.message.answer("Укажите причину отмены:")

    @router.message(VoidFlow.reason)
    async def void_reason(message: Message, state: FSMContext):
        data = await state.get_data()
        try:
            if data["void_type"] == "attendance":
                services.attendance.void(
                    data["void_id"], actor=message.from_user.id, reason=message.text
                )
                result = "Рабочий день отменён. История сохранена."
            else:
                services.payments.void(
                    data["void_id"], actor=message.from_user.id, reason=message.text
                )
                result = "Выплата отменена. История сохранена."
        except DomainError as exc:
            await message.answer(str(exc))
            return
        await state.clear()
        await message.answer(result, reply_markup=kb.MAIN)

    @router.message(F.text == "📊 Отчёты")
    async def reports_menu(message: Message, state: FSMContext):
        await state.clear()
        await message.answer("📊 Отчёты", reply_markup=kb.REPORTS)

    @router.callback_query(F.data == "report:month")
    async def month_report(callback: CallbackQuery):
        start_date, end_date = month_bounds(today())
        summaries = services.payroll.all_summaries(date_from=start_date, date_to=end_date)
        closing = {
            row.employee_id: services.payroll.summary(row.employee_id, date_to=end_date)
            for row in summaries
        }
        lines = [f"Общий отчёт {start_date.strftime('%m.%Y')}"]
        lines.extend(
            f"{row.employee_name}: {row.days} дн.; начислено {amount(row.earned, row.currency)}; "
            f"выплачено {amount(row.paid, row.currency)}; долг на конец "
            f"{amount(closing[row.employee_id].balance, row.currency)}"
            for row in summaries
        )
        for currency, (earned, paid) in sorted(totals_by_currency(summaries).items()):
            lines.append(
                f"Итого {currency}: начислено {amount(earned, currency)}, "
                f"выплачено {amount(paid, currency)}, остаток {amount(earned - paid, currency)}"
            )
        for currency, (earned, paid) in sorted(totals_by_currency(list(closing.values())).items()):
            lines.append(f"Общий долг {currency} на конец: {amount(earned - paid, currency)}")
        lines.append("\nРасходы по объектам:")
        for obj in services.objects.list(active_only=False):
            _, object_rows = services.reports.object_report(obj.id, start_date, end_date)
            object_totals: dict[str, Decimal] = {}
            for _, currency, _, earned in object_rows:
                object_totals[currency] = object_totals.get(currency, Decimal(0)) + Decimal(earned)
            if object_totals:
                rendered = ", ".join(
                    amount(value, currency) for currency, value in sorted(object_totals.items())
                )
                lines.append(f"{obj.name}: {rendered}")
        await callback.answer()
        await answer_long(callback.message, "\n".join(lines))

    @router.callback_query(F.data == "report:employee")
    async def employee_report_picker(callback: CallbackQuery):
        await callback.answer()
        await callback.message.answer(
            "Выберите сотрудника:",
            reply_markup=kb.entity_list(services.employees.list(), "reportemp"),
        )

    @router.callback_query(F.data.startswith("reportemp:"))
    async def employee_report(callback: CallbackQuery, state: FSMContext):
        await state.set_state(ReportFlow.period)
        await state.update_data(report_kind="employee", report_id=callback.data.split(":", 1)[1])
        await callback.answer()
        await callback.message.answer("Выберите период:", reply_markup=kb.REPORT_PERIOD)

    @router.callback_query(F.data == "report:object")
    async def object_report_picker(callback: CallbackQuery):
        await callback.answer()
        await callback.message.answer(
            "Выберите объект:",
            reply_markup=kb.entity_list(services.objects.list(active_only=False), "reportobj"),
        )

    @router.callback_query(F.data.startswith("reportobj:"))
    async def object_report(callback: CallbackQuery, state: FSMContext):
        await state.set_state(ReportFlow.period)
        await state.update_data(report_kind="object", report_id=callback.data.split(":", 1)[1])
        await callback.answer()
        await callback.message.answer("Выберите период:", reply_markup=kb.REPORT_PERIOD)

    async def render_selected_report(target: Message, state: FSMContext) -> None:
        data = await state.get_data()
        start_date = date.fromisoformat(data["report_from"])
        end_date = date.fromisoformat(data["report_to"])
        if data["report_kind"] == "employee":
            summary = services.payroll.summary(
                data["report_id"], date_from=start_date, date_to=end_date
            )
            attendance, payments = services.reports.employee_history(
                data["report_id"], start_date, end_date
            )
            objects = {row.id: row.name for row in services.objects.list(active_only=False)}
            lines = [f"{summary.employee_name}\n{start_date:%d.%m.%Y}–{end_date:%d.%m.%Y}"]
            lines.extend(
                f"{row.work_date:%d.%m} · {objects.get(row.object_id, 'Объект')} · "
                f"{row.coefficient} × {row.rate_snapshot} = "
                f"{amount(row.earned_amount, row.currency)}"
                for row in attendance
            )
            lines.extend(
                f"Выплата {row.payment_date:%d.%m}: {amount(row.amount, row.currency)}"
                for row in payments
            )
            lines.append(f"Остаток периода: {amount(summary.balance, summary.currency)}")
            closing = services.payroll.summary(data["report_id"], date_to=end_date)
            lines.append(
                f"Общий долг на конец периода: {amount(closing.balance, closing.currency)}"
            )
        else:
            obj, rows = services.reports.object_report(data["report_id"], start_date, end_date)
            lines = [f"{obj.name}\n{start_date:%d.%m.%Y}–{end_date:%d.%m.%Y}"]
            total_days = Decimal(0)
            totals: dict[str, Decimal] = {}
            for name, currency, days, earned in rows:
                lines.append(f"{name}: {days} дней — {amount(earned, currency)}")
                total_days += Decimal(days)
                totals[currency] = totals.get(currency, Decimal(0)) + Decimal(earned)
            lines.append(f"Итого: {total_days} человеко-дней")
            lines.extend(amount(value, currency) for currency, value in sorted(totals.items()))
        await state.set_state(ReportFlow.ready)
        await answer_long(target, "\n".join(lines), reply_markup=kb.REPORT_EXPORT)

    @router.callback_query(ReportFlow.period, F.data == "report:period:month")
    async def report_current_month(callback: CallbackQuery, state: FSMContext):
        start_date, end_date = month_bounds(today())
        await state.update_data(report_from=start_date.isoformat(), report_to=end_date.isoformat())
        await callback.answer()
        await render_selected_report(callback.message, state)

    @router.callback_query(ReportFlow.period, F.data == "report:period:custom")
    async def report_custom_period(callback: CallbackQuery, state: FSMContext):
        await state.set_state(ReportFlow.custom_from)
        await callback.answer()
        await callback.message.answer("Начало периода, ДД.ММ.ГГГГ:")

    @router.message(ReportFlow.custom_from)
    async def report_custom_from(message: Message, state: FSMContext):
        try:
            start_date = parse_date(message.text)
        except DomainError as exc:
            await message.answer(str(exc))
            return
        await state.update_data(report_from=start_date.isoformat())
        await state.set_state(ReportFlow.custom_to)
        await message.answer("Конец периода, ДД.ММ.ГГГГ:")

    @router.message(ReportFlow.custom_to)
    async def report_custom_to(message: Message, state: FSMContext):
        data = await state.get_data()
        try:
            end_date = parse_date(message.text)
            validate_report_period(date.fromisoformat(data["report_from"]), end_date)
        except DomainError as exc:
            await message.answer(str(exc))
            return
        await state.update_data(report_to=end_date.isoformat())
        await render_selected_report(message, state)

    @router.callback_query(ReportFlow.ready, F.data.startswith("exportctx:"))
    async def export_selected_report(callback: CallbackQuery, state: FSMContext):
        data = await state.get_data()
        start_date = date.fromisoformat(data["report_from"])
        end_date = date.fromisoformat(data["report_to"])
        export_kind = callback.data.split(":", 1)[1]
        if data["report_kind"] == "employee":
            employee = services.employees.get(data["report_id"])
            attendance, payments = services.reports.employee_history(
                employee.id, start_date, end_date
            )
            objects = {row.id: row.name for row in services.objects.list(active_only=False)}
            headers = ["Дата", "Тип", "Объект", "Коэффициент", "Ставка", "Сумма", "Валюта"]
            rows = [
                [
                    row.work_date.isoformat(),
                    "Начисление",
                    objects.get(row.object_id, ""),
                    row.coefficient,
                    row.rate_snapshot,
                    row.earned_amount,
                    row.currency,
                ]
                for row in attendance
            ]
            rows.extend(
                [
                    row.payment_date.isoformat(),
                    "Выплата",
                    objects.get(row.object_id, "") if row.object_id else "",
                    "",
                    "",
                    row.amount,
                    row.currency,
                ]
                for row in payments
            )
            title = employee.name
            filename = f"employee-{employee.id}-{start_date}-{end_date}"
        else:
            obj, report_rows = services.reports.object_report(
                data["report_id"], start_date, end_date
            )
            headers = ["Сотрудник", "Валюта", "Человеко-дни", "Начислено"]
            rows = [list(row) for row in report_rows]
            title = obj.name
            filename = f"object-{obj.id}-{start_date}-{end_date}"
        payload = (
            table_csv(headers, rows) if export_kind == "csv" else table_xlsx(title, headers, rows)
        )
        await callback.answer()
        await callback.message.answer_document(
            BufferedInputFile(payload, filename=f"{filename}.{export_kind}")
        )

    @router.callback_query(F.data.in_({"export:csv", "export:xlsx"}))
    async def export(callback: CallbackQuery):
        start_date, end_date = month_bounds(today())
        summaries = services.payroll.all_summaries(date_from=start_date, date_to=end_date)
        kind = callback.data.split(":", 1)[1]
        payload = summaries_csv(summaries) if kind == "csv" else summaries_xlsx(summaries)
        filename = f"payroll-{start_date:%Y-%m}.{kind}"
        await callback.answer()
        await callback.message.answer_document(BufferedInputFile(payload, filename=filename))

    @router.message(F.text == "⚙️ Настройки")
    async def settings(message: Message, state: FSMContext):
        await state.clear()
        await message.answer(
            f"Часовой пояс: {timezone_name}\nВалюта по умолчанию: {default_currency}\n"
            "У каждого сотрудника может быть своя валюта. Смешанные валюты не суммируются.\n"
            "Роль: OWNER"
        )

    return router
