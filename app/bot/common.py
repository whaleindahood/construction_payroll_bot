from __future__ import annotations

from dataclasses import dataclass
from datetime import date

from aiogram.types import Message

from app.services import (
    AttendanceService,
    DomainError,
    EmployeeService,
    ObjectService,
    PaymentService,
    PayrollService,
)


@dataclass(frozen=True)
class Services:
    employees: EmployeeService
    objects: ObjectService
    attendance: AttendanceService
    payroll: PayrollService
    payments: PaymentService


def parse_date(value: str) -> date:
    try:
        day, month, year = (int(part) for part in (value or "").strip().split("."))
        return date(year, month, day)
    except (TypeError, ValueError) as exc:
        raise DomainError("Введите дату в формате ДД.ММ.ГГГГ.") from exc


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
