from datetime import date
from decimal import Decimal

import pytest
from openpyxl import Workbook

from symbol_payroll.domain.pay import SalaryBasis
from symbol_payroll.infrastructure.database import connect, initialize
from symbol_payroll.infrastructure.excel_import import (
    ColumnMapping, apply_import_preview, build_import_preview, read_headers,
    suggest_mapping,
)
from symbol_payroll.infrastructure.payroll_repository import create_period
from symbol_payroll.infrastructure.repositories import create_employee, create_store
from symbol_payroll.infrastructure.period_inputs import (
    PeriodEmployeeInput, list_period_inputs, save_period_input,
)


@pytest.fixture
def db(tmp_path):
    connection = connect(tmp_path / "import.sqlite3")
    initialize(connection)
    create_store(connection, "s1", "Сигаево")
    create_employee(
        connection, "e1", "s1", "Иванов Иван", "Продавец",
        Decimal("30000"), SalaryBasis.GROSS,
    )
    create_employee(
        connection, "e2", "s1", "Петров Пётр", "Продавец",
        Decimal("25000"), SalaryBasis.UNCONFIRMED,
    )
    create_period(connection, "2026-10", date(2026, 10, 1), date(2026, 10, 31))
    yield connection
    connection.close()


def workbook_file(tmp_path, rows):
    path = tmp_path / "payroll.xlsx"
    workbook = Workbook()
    sheet = workbook.active
    sheet.title = "Сигаево"
    for row in rows:
        sheet.append(row)
    workbook.save(path)
    return path


def test_suggest_mapping_and_preview_resolve_employee_names(db, tmp_path) -> None:
    path = workbook_file(tmp_path, [
        ["ФИО сотрудника", "Смены", "Часы", "Продажи", "Аванс"],
        ["Иванов Иван", 15, 120, 250000, 5000],
    ])
    headers = read_headers(path, "Сигаево")
    mapping = suggest_mapping(headers)
    assert mapping.employee_name == 0
    assert mapping.shifts == 1
    preview = build_import_preview(db, path, "Сигаево", "s1", 1, mapping)
    assert preview.valid_count == 1
    assert preview.errors == ()
    assert preview.rows[0].employee_id == "e1"
    assert preview.rows[0].values["sales"] == Decimal("250000")


def test_apply_preview_imports_atomically_to_period_inputs(db, tmp_path) -> None:
    path = workbook_file(tmp_path, [
        ["Сотрудник", "Смены", "Продажи", "НДФЛ"],
        ["Иванов Иван", 12, 190000, 24700],
        ["Петров Пётр", 10, 150000, 0],
    ])
    mapping = suggest_mapping(read_headers(path, "Сигаево"))
    preview = build_import_preview(db, path, "Сигаево", "s1", 1, mapping)
    assert apply_import_preview(db, "2026-10", preview) == 2
    imported = list_period_inputs(db, "2026-10")
    assert imported["e1"].shifts == Decimal("12")
    assert imported["e1"].sales == Decimal("190000")
    assert imported["e1"].manual_tax == Decimal("24700")


def test_preview_rejects_unknown_employee_and_negative_values(db, tmp_path) -> None:
    path = workbook_file(tmp_path, [
        ["Сотрудник", "Смены", "Удержания"],
        ["Неизвестный", 10, 100],
        ["Иванов Иван", 10, -50],
    ])
    preview = build_import_preview(
        db, path, "Сигаево", "s1", 1,
        suggest_mapping(read_headers(path, "Сигаево")),
    )
    assert len(preview.errors) == 2
    with pytest.raises(ValueError, match="Импорт отменён"):
        apply_import_preview(db, "2026-10", preview)
    assert list_period_inputs(db, "2026-10") == {}


def test_preview_rejects_duplicate_employee_rows(db, tmp_path) -> None:
    path = workbook_file(tmp_path, [
        ["ФИО", "Смены"],
        ["Иванов Иван", 10],
        ["Иванов Иван", 11],
    ])
    preview = build_import_preview(
        db, path, "Сигаево", "s1", 1,
        suggest_mapping(read_headers(path, "Сигаево")),
    )
    assert any("повторяется" in error for error in preview.errors)


def test_import_rejects_approved_period(db, tmp_path) -> None:
    path = workbook_file(tmp_path, [
        ["Сотрудник", "Смены"],
        ["Иванов Иван", 10],
    ])
    preview = build_import_preview(
        db, path, "Сигаево", "s1", 1,
        suggest_mapping(read_headers(path, "Сигаево")),
    )
    db.execute("UPDATE payroll_periods SET status='approved' WHERE id='2026-10'")
    with pytest.raises(ValueError, match="утверждённый"):
        apply_import_preview(db, "2026-10", preview)



def test_formula_without_cached_result_is_rejected(db, tmp_path) -> None:
    path = workbook_file(tmp_path, [
        ["Сотрудник", "Смены"],
        ["Иванов Иван", "=1+2"],
    ])
    preview = build_import_preview(
        db, path, "Сигаево", "s1", 1,
        suggest_mapping(read_headers(path, "Сигаево")),
    )
    assert any("не содержит сохранённого результата" in error for error in preview.errors)


def test_import_resets_calculated_period_and_removes_old_snapshots(db, tmp_path) -> None:
    path = workbook_file(tmp_path, [
        ["Сотрудник", "Смены"],
        ["Иванов Иван", 9],
    ])
    preview = build_import_preview(
        db, path, "Сигаево", "s1", 1,
        suggest_mapping(read_headers(path, "Сигаево")),
    )
    db.execute("UPDATE payroll_periods SET status='calculated' WHERE id='2026-10'")
    db.execute(
        """INSERT INTO payroll_snapshots
           (id, period_id, employee_id, snapshot_json, rules_fingerprint, created_at)
           VALUES ('snap1', '2026-10', 'e1', '{}', 'old', '2026-10-31T00:00:00Z')"""
    )
    assert apply_import_preview(db, "2026-10", preview) == 1
    assert db.execute(
        "SELECT status FROM payroll_periods WHERE id='2026-10'"
    ).fetchone()["status"] == "draft"
    assert db.execute(
        "SELECT COUNT(*) FROM payroll_snapshots WHERE period_id='2026-10'"
    ).fetchone()[0] == 0



def test_unmapped_columns_preserve_existing_period_values(db, tmp_path) -> None:
    save_period_input(
        db, PeriodEmployeeInput(
            "2026-10", "e1", shifts=Decimal("4"), hours=Decimal("38"),
            sales=Decimal("90000"), extra_values={"commission_rate": Decimal("0.03")},
        )
    )
    path = workbook_file(tmp_path, [
        ["Сотрудник", "Смены"],
        ["Иванов Иван", 12],
    ])
    preview = build_import_preview(
        db, path, "Сигаево", "s1", 1,
        suggest_mapping(read_headers(path, "Сигаево")),
    )
    assert preview.rows[0].values["hours"] is None
    apply_import_preview(db, "2026-10", preview)
    imported = list_period_inputs(db, "2026-10")["e1"]
    assert imported.shifts == Decimal("12")
    assert imported.hours == Decimal("38")
    assert imported.sales == Decimal("90000")
    assert imported.extra_values["commission_rate"] == Decimal("0.03")
