from datetime import date
from decimal import Decimal

import pytest

from symbol_payroll.domain.pay import SalaryBasis
from symbol_payroll.infrastructure.database import connect, initialize
from symbol_payroll.infrastructure.payroll_repository import create_period, list_period_snapshots
from symbol_payroll.infrastructure.period_inputs import PeriodEmployeeInput, save_period_input
from symbol_payroll.infrastructure.repositories import (
    create_employee, create_store, update_employee,
)
from symbol_payroll.services.payroll_service import calculate_period
from symbol_payroll.services.termination_service import (
    approve_termination_case,
    calculate_termination_case,
    create_termination_case,
)


@pytest.fixture
def db(tmp_path):
    connection = connect(tmp_path / "termination.sqlite3")
    initialize(connection)
    create_store(connection, "store-1", "Сигаево")
    create_employee(
        connection, "employee-1", "store-1", "Иванов Иван",
        position="Продавец", salary_amount=Decimal("90000"),
        salary_basis=SalaryBasis.GROSS,
        hired_on=date(2020, 1, 1),
        salary_effective_from=date(2020, 1, 1),
    )
    yield connection
    connection.close()


def test_termination_settlement_uses_only_explicit_final_components(db):
    case_id = create_termination_case(
        db, "employee-1", date(2026, 10, 15),
        worked_shifts=Decimal("12"),
        worked_hours=Decimal("96"),
        advances=Decimal("5000"),
        other_deductions=Decimal("1000"),
        manual_tax=Decimal("3900"),
        components=[
            {
                "code": "worked_salary",
                "title": "Оплата фактически отработанного времени",
                "amount": "30000",
                "category": "earning",
            }
        ],
        notes="Контрольный расчёт",
    )

    result = calculate_termination_case(db, case_id)

    assert result["gross_total"] == "30000.00"
    assert result["tax_amount"] == "3900.00"
    assert result["deduction_total"] == "9900.00"
    assert result["net_total"] == "20100.00"
    assert "автоматически не начислялись" in result["calculation_note"]
    assert db.execute(
        "SELECT active FROM employees WHERE id='employee-1'"
    ).fetchone()["active"] == 0


def test_termination_rejects_deductions_above_final_payable(db):
    case_id = create_termination_case(
        db, "employee-1", date(2026, 10, 15),
        advances=Decimal("5000"),
        components=[{
            "code": "final_pay", "title": "Окончательное начисление",
            "amount": "3000", "category": "earning",
        }],
    )
    with pytest.raises(ValueError, match="Удержания превышают"):
        calculate_termination_case(db, case_id)


def test_approved_termination_is_immutable(db):
    case_id = create_termination_case(
        db, "employee-1", date(2026, 10, 15),
        components=[{
            "code": "final_pay", "title": "Окончательное начисление",
            "amount": "30000", "category": "earning",
        }],
    )
    calculate_termination_case(db, case_id)
    approve_termination_case(db, case_id)

    with pytest.raises(ValueError, match="нельзя пересчитывать"):
        calculate_termination_case(db, case_id)
    with pytest.raises(Exception, match="immutable"):
        db.execute(
            "UPDATE termination_cases SET notes='изменено' WHERE id=?", (case_id,)
        )


def test_terminated_employee_remains_in_historical_period_only(db):
    create_termination_case(db, "employee-1", date(2026, 10, 15))
    create_period(db, "2026-10", date(2026, 10, 1), date(2026, 10, 31))
    create_period(db, "2026-11", date(2026, 11, 1), date(2026, 11, 30))

    october = calculate_period(db, "2026-10")
    assert [item.employee_id for item in october.employees] == ["employee-1"]
    assert october.employees[0].gross_total == Decimal("0.00")
    assert any("полный месячный оклад автоматически не начислен" in warning
               for warning in october.warnings)

    with pytest.raises(ValueError, match="Нет сотрудников"):
        calculate_period(db, "2026-11")


def test_termination_date_and_component_validation(db):
    with pytest.raises(ValueError, match="неотрицательной"):
        create_termination_case(
            db, "employee-1", date(2026, 10, 15), advances=Decimal("-1")
        )



def test_schema_v3_migrates_to_v4_with_termination_table(db):
    db.execute("UPDATE schema_meta SET version=3")
    initialize(db)
    assert db.execute("SELECT version FROM schema_meta").fetchone()["version"] == 5
    columns = {
        row["name"] for row in db.execute("PRAGMA table_info(termination_cases)")
    }
    assert {"termination_date", "components_json", "result_json", "status"} <= columns



def test_termination_invalidates_calculated_monthly_payroll(db) -> None:
    create_period(db, "2026-10", date(2026, 10, 1), date(2026, 10, 31))
    save_period_input(
        db, PeriodEmployeeInput("2026-10", "employee-1", shifts=Decimal("12"))
    )
    calculate_period(db, "2026-10")
    assert len(list_period_snapshots(db, "2026-10")) == 1

    create_termination_case(db, "employee-1", date(2026, 10, 15))

    assert db.execute(
        "SELECT status FROM payroll_periods WHERE id='2026-10'"
    ).fetchone()["status"] == "draft"
    assert list_period_snapshots(db, "2026-10") == []


def test_terminated_employee_cannot_be_reactivated(db) -> None:
    create_termination_case(db, "employee-1", date(2026, 10, 15))

    with pytest.raises(ValueError, match="повторно активировать"):
        update_employee(
            db, "employee-1", "store-1", "Иванов Иван",
            position="Продавец", salary_amount=Decimal("90000"),
            salary_basis=SalaryBasis.GROSS, active=True,
        )

    with pytest.raises(Exception, match="Terminated employees cannot be reactivated"):
        db.execute("UPDATE employees SET active=1 WHERE id='employee-1'")



def test_hire_date_is_persisted_in_termination_snapshot_history(db) -> None:
    employee = db.execute(
        "SELECT hired_on FROM employees WHERE id='employee-1'"
    ).fetchone()
    assert employee["hired_on"] == "2020-01-01"



def test_termination_date_cannot_precede_hire_date(db) -> None:
    with pytest.raises(ValueError, match="раньше даты приёма"):
        create_termination_case(db, "employee-1", date(2019, 12, 31))
