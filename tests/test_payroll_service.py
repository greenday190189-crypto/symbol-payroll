from datetime import date
from decimal import Decimal

import pytest

from symbol_payroll.domain.pay import SalaryBasis
from symbol_payroll.domain.rules import RuleScope
from symbol_payroll.domain.tax import TaxMode
from symbol_payroll.infrastructure.database import connect, initialize
from symbol_payroll.infrastructure.payroll_repository import (
    create_period, list_period_snapshots, save_rule_version, transition_period,
)
from symbol_payroll.infrastructure.period_inputs import (
    PeriodEmployeeInput, list_period_inputs, save_period_input,
)
from symbol_payroll.infrastructure.repositories import create_employee, create_store
from symbol_payroll.services.payroll_service import calculate_period


@pytest.fixture
def db(tmp_path):
    connection = connect(tmp_path / "payroll.sqlite3")
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


def test_period_input_round_trip(db) -> None:
    item = PeriodEmployeeInput(
        "2026-10", "e1", shifts=Decimal("15"), hours=Decimal("120"),
        sales=Decimal("250000"), plan=Decimal("240000"),
        advances=Decimal("5000"), deductions=Decimal("1000"),
        manual_tax=Decimal("3900"), fixed_adjustments=Decimal("500"),
        extra_values={"conversion": Decimal("0.15")},
    )
    save_period_input(db, item)
    loaded = list_period_inputs(db, "2026-10")["e1"]
    assert loaded.shifts == Decimal("15")
    assert loaded.sales == Decimal("250000")
    assert loaded.extra_values["conversion"] == Decimal("0.15")


def test_period_input_rejects_negative_operational_values(db) -> None:
    with pytest.raises(ValueError, match="неотрицательным"):
        save_period_input(
            db, PeriodEmployeeInput("2026-10", "e1", advances=Decimal("-1"))
        )


def test_orchestration_calculates_and_persists_all_active_employees(db) -> None:
    save_period_input(
        db, PeriodEmployeeInput(
            "2026-10", "e1", shifts=Decimal("15"), hours=Decimal("120"),
            advances=Decimal("5000"), deductions=Decimal("1000"),
        )
    )
    save_rule_version(
        db, "sales_bonus", RuleScope.STORE, "s1",
        "sales * 0.01", date(2026, 1, 1), category="bonus", title="Премия с продаж",
    )
    save_period_input(
        db, PeriodEmployeeInput("2026-10", "e1", sales=Decimal("100000"),
                                shifts=Decimal("15"), hours=Decimal("120"),
                                advances=Decimal("5000"), deductions=Decimal("1000"))
    )
    result = calculate_period(db, "2026-10", TaxMode.DISABLED)
    assert len(result.employees) == 2
    first = next(item for item in result.employees if item.employee_id == "e1")
    assert first.gross_total == Decimal("31000.00")
    assert first.deduction_total == Decimal("6000.00")
    assert first.net_total == Decimal("25000.00")
    second = next(item for item in result.employees if item.employee_id == "e2")
    assert second.gross_total == Decimal("0.00")
    assert second.net_total == Decimal("0.00")
    assert any("тип оклада gross/net не определён" in warning for warning in result.warnings)
    snapshots = list_period_snapshots(db, "2026-10")
    assert len(snapshots) == 2
    assert db.execute(
        "SELECT status FROM payroll_periods WHERE id='2026-10'"
    ).fetchone()["status"] == "calculated"


def test_manual_tax_mode_is_calculated_from_period_input(db) -> None:
    save_period_input(
        db, PeriodEmployeeInput("2026-10", "e1", manual_tax=Decimal("3900"))
    )
    result = calculate_period(db, "2026-10", TaxMode.MANUAL)
    first = next(item for item in result.employees if item.employee_id == "e1")
    assert first.tax_amount == Decimal("3900.00")
    assert first.net_total == Decimal("26100.00")


def test_period_cannot_be_recalculated_after_approval(db) -> None:
    save_period_input(
        db, PeriodEmployeeInput("2026-10", "e1", shifts=Decimal("10"))
    )
    calculate_period(db, "2026-10")
    transition_period(db, "2026-10", "approved")
    with pytest.raises(ValueError, match="Можно рассчитывать только"):
        calculate_period(db, "2026-10")
    with pytest.raises(Exception, match="Approved payroll inputs are immutable"):
        db.execute(
            "UPDATE payroll_inputs SET shifts='99' WHERE period_id='2026-10' AND employee_id='e1'"
        )



def test_schema_v2_migrates_to_v3_with_rule_metadata_and_inputs(tmp_path) -> None:
    from symbol_payroll.infrastructure.database import connect, initialize

    connection = connect(tmp_path / "legacy.sqlite3")
    connection.executescript(
        """
        CREATE TABLE schema_meta (version INTEGER NOT NULL);
        INSERT INTO schema_meta(version) VALUES (2);
        CREATE TABLE payroll_rules (
            id TEXT PRIMARY KEY, code TEXT NOT NULL,
            scope TEXT NOT NULL, scope_id TEXT NOT NULL,
            expression TEXT NOT NULL, version INTEGER NOT NULL,
            effective_from TEXT NOT NULL, effective_to TEXT,
            enabled INTEGER NOT NULL DEFAULT 1,
            UNIQUE(code, scope, scope_id, version)
        );
        """
    )
    initialize(connection)
    columns = {
        row["name"] for row in connection.execute("PRAGMA table_info(payroll_rules)")
    }
    assert {"category", "title"} <= columns
    assert connection.execute(
        "SELECT version FROM schema_meta"
    ).fetchone()["version"] == 3
    assert connection.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='payroll_inputs'"
    ).fetchone() is not None
    connection.close()



def test_editing_inputs_invalidates_previous_calculation(db) -> None:
    save_period_input(
        db, PeriodEmployeeInput("2026-10", "e1", shifts=Decimal("10"))
    )
    calculate_period(db, "2026-10")
    assert len(list_period_snapshots(db, "2026-10")) == 2
    save_period_input(
        db, PeriodEmployeeInput("2026-10", "e1", shifts=Decimal("11"))
    )
    assert list_period_snapshots(db, "2026-10") == []
    assert db.execute(
        "SELECT status FROM payroll_periods WHERE id='2026-10'"
    ).fetchone()["status"] == "draft"
