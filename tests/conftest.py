from __future__ import annotations

import pytest

from app.db import create_schema, make_session_factory
from app.services import (
    AttendanceService,
    EmployeeService,
    ObjectService,
    PaymentService,
    PayrollService,
)


@pytest.fixture
def services(tmp_path):
    sessions = make_session_factory(f"sqlite:///{tmp_path / 'test.db'}")
    create_schema(sessions)
    payroll = PayrollService(sessions)
    return {
        "sessions": sessions,
        "employees": EmployeeService(sessions),
        "objects": ObjectService(sessions),
        "attendance": AttendanceService(sessions),
        "payroll": payroll,
        "payments": PaymentService(sessions),
    }
