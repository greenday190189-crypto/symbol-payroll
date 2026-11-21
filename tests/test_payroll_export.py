from datetime import date
from decimal import Decimal

import pytest
from openpyxl import load_workbook

from symbol_payroll.infrastructure.database import connect, initialize
from symbol_payroll.infrastructure.payroll_export import export_period_workbook
from symbol_payroll.infrastructure.payroll_repository import create_period, save_employee_snapshot
from symbol_payroll.infrastructure.repositories import create_employee, create_store


@pytest.fixture
def db(tmp_path):
    connection = connect(tmp_path / "export.sqlite3")
    initialize(connection)
    create_store(connection, "s1", "Сигаево")
    create_store(connection, "s2", "Дачная")
    create_employee(connection, "e1", "s1", "Иванов Иван")
    create_employee(connection, "e2", "s2", "Петров Пётр")
    create_period(connection, "2026-10", date(2026, 10, 1), date(2026, 10, 31))
    yield connection
    connection.close()


def _snapshot(employee_id, name, gross, tax, deductions, net):
    return {
        "employee_id": employee_id,
        "full_name": name,
        "components": [
            {"code": "base", "title": "Оклад", "amount": gross, "category": "earning"},
            {"code": "tax", "title": "НДФЛ", "amount": tax, "category": "deduction"},
        ],
        "gross_total": gross,
        "tax_amount": tax,
        "deduction_total": deductions,
        "net_total": net,
        "warnings": [],
    }


def test_export_contains_network_store_and_personal_sheets(db, tmp_path) -> None:
    save_employee_snapshot(
        db, "2026-10", "e1", _snapshot("e1", "Иванов Иван", "50000.00", "6500.00", "6500.00", "43500.00"),
        ["salary:v1"],
    )
    save_employee_snapshot(
        db, "2026-10", "e2", _snapshot("e2", "Петров Пётр", "40000.00", "5200.00", "5200.00", "34800.00"),
        ["salary:v1"],
    )
    db.execute("UPDATE payroll_periods SET status='approved' WHERE id='2026-10'")
    path = export_period_workbook(db, "2026-10", tmp_path / "payroll.xlsx")
    workbook = load_workbook(path, data_only=False)

    assert {"Сводная", "По магазинам", "Сигаево", "Дачная"} <= set(workbook.sheetnames)
    assert any(name.startswith("Иванов Иван") for name in workbook.sheetnames)
    assert any(name.startswith("Петров Пётр") for name in workbook.sheetnames)
    summary = workbook["Сводная"]
    assert summary["D1"].value == "УТВЕРЖДЕНО"
    assert summary["B6"].value == 50000.0
    assert summary["B7"].value == 40000.0
    assert summary["B8"].value == "=SUM(B6:B7)"
    assert workbook["По магазинам"]["A2"].value == "Дачная"
    assert workbook["По магазинам"]["C2"].value == 40000.0
    store_sheet = workbook["Сигаево"]
    assert store_sheet["A3"].value == "Магазин: Сигаево"
    assert store_sheet["B5"].value == 50000.0
    employee_sheet = next(
        workbook[name] for name in workbook.sheetnames if name.startswith("Иванов Иван")
    )
    assert employee_sheet["B2"].value == "Иванов Иван"
    assert employee_sheet["A6"].value == "Оклад"
    assert employee_sheet["C6"].value == 50000.0
    workbook.close()


def test_export_requires_saved_snapshots(db, tmp_path) -> None:
    with pytest.raises(ValueError, match="нет сохранённых"):
        export_period_workbook(db, "2026-10", tmp_path / "empty.xlsx")


def test_export_rejects_unknown_period(db, tmp_path) -> None:
    with pytest.raises(LookupError, match="не найден"):
        export_period_workbook(db, "missing", tmp_path / "missing.xlsx")
