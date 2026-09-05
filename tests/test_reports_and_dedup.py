from __future__ import annotations

from datetime import date

from openpyxl import load_workbook

from app.reports import table_csv, table_xlsx
from app.services import UpdateDedupService
from tests.test_financial_invariants import ACTOR, setup_employee_object


def test_object_report_and_exports(services):
    employee, obj = setup_employee_object(services)
    services["attendance"].create_bulk(
        employee_ids=[employee.id],
        object_id=obj.id,
        work_date=date(2026, 9, 4),
        coefficient="0.5",
        actor=ACTOR,
        operation_key="report-day",
    )
    summary = services["payroll"].summary(employee.id, object_id=obj.id)
    rows = [[summary.employee_name, summary.currency, summary.earned, summary.paid]]
    headers = ["Employee", "Currency", "Earned", "Paid"]
    csv_data = table_csv(headers, rows)
    assert employee.name.encode("utf-8") in csv_data
    workbook = load_workbook(filename=__import__("io").BytesIO(table_xlsx(obj.name, headers, rows)))
    assert workbook.active["A2"].value == employee.name
    assert workbook.active["B2"].value == employee.currency
    assert workbook.active["C2"].value == float(summary.earned)


def test_telegram_update_claim_is_idempotent(services):
    dedup = UpdateDedupService(services["sessions"])

    assert dedup.claim(77) is True
    assert dedup.claim(77) is False
    dedup.done(77)
    assert dedup.claim(77) is False


def test_exports_neutralize_spreadsheet_formulas():
    csv_data = table_csv(["Name"], [['=HYPERLINK("bad")']]).decode("utf-8")
    workbook = load_workbook(
        filename=__import__("io").BytesIO(table_xlsx("Bad/Title", ["Name"], [["+cmd"]]))
    )

    assert "'=HYPERLINK" in csv_data
    assert workbook.active.title == "Bad_Title"
    assert workbook.active["A2"].value == "'+cmd"
