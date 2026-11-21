from datetime import date
from decimal import Decimal

import pytest
from openpyxl import load_workbook

from symbol_payroll.domain.pay import SalaryBasis
from symbol_payroll.infrastructure.database import connect, initialize
from symbol_payroll.infrastructure.repositories import create_employee, create_store
from symbol_payroll.infrastructure.termination_export import export_termination_workbook
from symbol_payroll.services.termination_service import (
    approve_termination_case, calculate_termination_case, create_termination_case,
)


@pytest.fixture
def db(tmp_path):
    connection = connect(tmp_path / "termination-export.sqlite3")
    initialize(connection)
    create_store(connection, "s1", "Сигаево")
    create_employee(
        connection, "e1", "s1", "Иванов Иван", position="Продавец",
        salary_amount=Decimal("90000"), salary_basis=SalaryBasis.GROSS,
    )
    yield connection
    connection.close()


def _case(db):
    return create_termination_case(
        db, "e1", date(2026, 10, 15),
        worked_shifts=Decimal("12"), worked_hours=Decimal("96"),
        advances=Decimal("5000"), other_deductions=Decimal("1000"),
        manual_tax=Decimal("3900"),
        components=[{
            "code": "worked_salary",
            "title": "Оплата фактически отработанного времени",
            "amount": "30000",
            "category": "earning",
        }],
        notes="Контрольный расчёт",
    )


def test_export_termination_uses_saved_result_and_contains_details(db, tmp_path):
    case_id = _case(db)
    calculate_termination_case(db, case_id)
    path = export_termination_workbook(db, case_id, tmp_path / "termination.xlsx")
    workbook = load_workbook(path, data_only=False)
    sheet = workbook["Окончательный расчёт"]

    assert sheet["D1"].value == "РАССЧИТАН — НЕ УТВЕРЖДЁН"
    assert sheet["B2"].value == "Иванов Иван"
    assert sheet["D3"].value == "2026-10-15"
    assert sheet["A9"].value == "Оплата фактически отработанного времени"
    assert sheet["C9"].value == 30000.0
    assert sheet["B15"].value == 20100.0
    assert "автоматически не начислялись" in sheet["B17"].value
    workbook.close()


def test_export_termination_marks_approved_status(db, tmp_path):
    case_id = _case(db)
    calculate_termination_case(db, case_id)
    approve_termination_case(db, case_id)
    path = export_termination_workbook(db, case_id, tmp_path / "approved.xlsx")
    workbook = load_workbook(path, data_only=True)
    assert workbook["Окончательный расчёт"]["D1"].value == "УТВЕРЖДЁН"
    workbook.close()


def test_export_termination_requires_saved_calculation(db, tmp_path):
    case_id = _case(db)
    with pytest.raises(ValueError, match="Сначала рассчитайте"):
        export_termination_workbook(db, case_id, tmp_path / "draft.xlsx")


def test_export_termination_rejects_unknown_case(db, tmp_path):
    with pytest.raises(LookupError, match="не найдено"):
        export_termination_workbook(db, "missing", tmp_path / "missing.xlsx")
