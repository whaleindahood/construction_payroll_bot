from __future__ import annotations

import csv
import re
from io import BytesIO, StringIO

from openpyxl import Workbook

from .services import PayrollSummary

HEADERS = ["Сотрудник", "Дни", "Начислено", "Выплачено", "Баланс периода", "Валюта"]


def spreadsheet_safe(value):
    if isinstance(value, str) and value.startswith(("=", "+", "-", "@")):
        return f"'{value}"
    return value


def summary_rows(items: list[PayrollSummary]) -> list[list[str]]:
    return [
        [
            item.employee_name,
            str(item.days),
            str(item.earned),
            str(item.paid),
            str(item.balance),
            item.currency,
        ]
        for item in items
    ]


def summaries_csv(items: list[PayrollSummary]) -> bytes:
    return table_csv(HEADERS, summary_rows(items))


def summaries_xlsx(items: list[PayrollSummary]) -> bytes:
    rows = [
        [
            item.employee_name,
            item.days,
            item.earned,
            item.paid,
            item.balance,
            item.currency,
        ]
        for item in items
    ]
    return table_xlsx("Зарплаты", HEADERS, rows)


def table_csv(headers: list[str], rows: list[list]) -> bytes:
    stream = StringIO(newline="")
    writer = csv.writer(stream)
    writer.writerow(headers)
    writer.writerows([[spreadsheet_safe(value) for value in row] for row in rows])
    return ("\ufeff" + stream.getvalue()).encode("utf-8")


def table_xlsx(title: str, headers: list[str], rows: list[list]) -> bytes:
    book = Workbook()
    sheet = book.active
    sheet.title = re.sub(r"[\\/*?:\[\]]", "_", title)[:31] or "Report"
    sheet.append(headers)
    for row in rows:
        sheet.append([spreadsheet_safe(value) for value in row])
    sheet.freeze_panes = "A2"
    for column in sheet.columns:
        sheet.column_dimensions[column[0].column_letter].width = (
            max(len(str(cell.value or "")) for cell in column) + 2
        )
    stream = BytesIO()
    book.save(stream)
    return stream.getvalue()
