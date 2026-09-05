from __future__ import annotations

from aiogram.types import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    KeyboardButton,
    ReplyKeyboardMarkup,
)

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
