from __future__ import annotations

from aiogram.types import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    KeyboardButton,
    ReplyKeyboardMarkup,
)
from aiogram.utils.keyboard import InlineKeyboardBuilder

MAIN = ReplyKeyboardMarkup(
    keyboard=[
        [KeyboardButton(text="🏗 Объекты"), KeyboardButton(text="👷 База сотрудников")],
    ],
    resize_keyboard=True,
)


def one_button(text: str, callback: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[[InlineKeyboardButton(text=text, callback_data=callback)]]
    )


def entity_list(items, prefix: str, *, create_callback: str | None = None) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    for item in items:
        builder.button(text=item.name, callback_data=f"{prefix}:{item.id}")
    if create_callback:
        builder.button(text="➕ Добавить", callback_data=create_callback)
    builder.adjust(1)
    return builder.as_markup()


def dates(prefix: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="Сегодня", callback_data=f"{prefix}:today"),
                InlineKeyboardButton(text="Вчера", callback_data=f"{prefix}:yesterday"),
            ],
            [InlineKeyboardButton(text="Выбрать дату", callback_data=f"{prefix}:custom")],
            [InlineKeyboardButton(text="Отмена", callback_data="cancel")],
        ]
    )


def employee_picker(items, selected: set[str]) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    for item in items:
        mark = "☑" if item.id in selected else "☐"
        builder.button(text=f"{mark} {item.name}", callback_data=f"att:toggle:{item.id}")
    builder.button(text="Продолжить", callback_data="att:selected")
    builder.button(text="Отмена", callback_data="cancel")
    builder.adjust(1)
    return builder.as_markup()


COEFFICIENTS = InlineKeyboardMarkup(
    inline_keyboard=[
        [
            InlineKeyboardButton(text="Полный день", callback_data="att:coef:1"),
            InlineKeyboardButton(text="½ дня", callback_data="att:coef:0.5"),
        ],
        [InlineKeyboardButton(text="Другой всем", callback_data="att:coef:custom")],
        [InlineKeyboardButton(text="Указать отдельно", callback_data="att:coef:individual")],
        [InlineKeyboardButton(text="Отмена", callback_data="cancel")],
    ]
)


CONFIRM_ATTENDANCE = InlineKeyboardMarkup(
    inline_keyboard=[
        [InlineKeyboardButton(text="✅ Сохранить", callback_data="att:save")],
        [InlineKeyboardButton(text="✏️ Изменить", callback_data="att:change")],
        [InlineKeyboardButton(text="❌ Отмена", callback_data="cancel")],
    ]
)


PAYMENT_METHODS = InlineKeyboardMarkup(
    inline_keyboard=[
        [InlineKeyboardButton(text="Наличные", callback_data="pay:method:cash")],
        [InlineKeyboardButton(text="Банк", callback_data="pay:method:bank")],
        [InlineKeyboardButton(text="Другое", callback_data="pay:method:other")],
        [InlineKeyboardButton(text="Отмена", callback_data="cancel")],
    ]
)


CONFIRM_PAYMENT = InlineKeyboardMarkup(
    inline_keyboard=[
        [InlineKeyboardButton(text="✅ Подтвердить", callback_data="pay:save")],
        [InlineKeyboardButton(text="❌ Отмена", callback_data="cancel")],
    ]
)


REPORTS = InlineKeyboardMarkup(
    inline_keyboard=[
        [InlineKeyboardButton(text="Общий отчёт за месяц", callback_data="report:month")],
        [InlineKeyboardButton(text="По сотруднику", callback_data="report:employee")],
        [InlineKeyboardButton(text="По объекту", callback_data="report:object")],
        [
            InlineKeyboardButton(text="CSV", callback_data="export:csv"),
            InlineKeyboardButton(text="XLSX", callback_data="export:xlsx"),
        ],
    ]
)

REPORT_PERIOD = InlineKeyboardMarkup(
    inline_keyboard=[
        [InlineKeyboardButton(text="Текущий месяц", callback_data="report:period:month")],
        [InlineKeyboardButton(text="Другой период", callback_data="report:period:custom")],
        [InlineKeyboardButton(text="Отмена", callback_data="cancel")],
    ]
)

REPORT_EXPORT = InlineKeyboardMarkup(
    inline_keyboard=[
        [
            InlineKeyboardButton(text="CSV", callback_data="exportctx:csv"),
            InlineKeyboardButton(text="XLSX", callback_data="exportctx:xlsx"),
        ]
    ]
)
