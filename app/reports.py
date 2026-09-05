from __future__ import annotations

import csv
import re
from io import BytesIO, StringIO

from openpyxl import Workbook


def spreadsheet_safe(value):
    if isinstance(value, str) and value.startswith(("=", "+", "-", "@")):
        return f"'{value}"
    return value


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
