from decimal import Decimal

import pytest

from symbol_payroll.domain.pay import SalaryBasis
from symbol_payroll.infrastructure.database import connect, initialize
from symbol_payroll.infrastructure.repositories import (
    create_employee, create_store, list_employees, list_stores, set_employee_active,
    update_employee,
)


@pytest.fixture
def db(tmp_path):
    connection = connect(tmp_path / "test.sqlite3")
    initialize(connection)
    yield connection
    connection.close()


def test_store_and_employee_crud(db) -> None:
    create_store(db, "s1", "Сигаево")
    employee = create_employee(
        db, "e1", "s1", "Иванов Иван", "Продавец", Decimal("42000"), SalaryBasis.GROSS
    )
    assert list_stores(db)[0].name == "Сигаево"
    assert list_employees(db, "s1")[0] == employee
    set_employee_active(db, "e1", False)
    assert list_employees(db, "s1") == []
    assert list_employees(db, "s1", include_inactive=True)[0].active is False


def test_employee_requires_existing_store(db) -> None:
    with pytest.raises(Exception):
        create_employee(db, "e1", "missing", "Иванов Иван")


def test_blank_names_are_rejected(db) -> None:
    with pytest.raises(ValueError, match="Название магазина"):
        create_store(db, "s1", "  ")
    with pytest.raises(ValueError, match="ФИО"):
        create_employee(db, "e1", "s1", "  ")


def test_update_employee_invalidates_unapproved_calculation_and_preserves_approved_history(db) -> None:
    create_store(db, "s1", "Сигаево")
    create_employee(
        db, "e1", "s1", "Иванов Иван", "Продавец", Decimal("42000"), SalaryBasis.UNCONFIRMED
    )
    for period_id, status in (("p-draft", "calculated"), ("p-approved", "approved")):
        db.execute(
            """INSERT INTO payroll_periods
               (id, period_start, period_end, status, created_at)
               VALUES (?, '2026-10-01', '2026-10-31', 'draft', '2026-11-01T00:00:00+00:00')""",
            (period_id,),
        )
        db.execute(
            """INSERT INTO payroll_snapshots
               (id, period_id, employee_id, snapshot_json, rules_fingerprint, created_at)
               VALUES (?, ?, 'e1', '{"full_name":"Иванов Иван"}', 'fingerprint', '2026-11-01T00:00:00+00:00')""",
            (f"snapshot-{period_id}", period_id),
        )
        db.execute("UPDATE payroll_periods SET status = ? WHERE id = ?", (status, period_id))

    updated = update_employee(
        db, "e1", "s1", "Иванов Иван", "Администратор",
        Decimal("50000"), SalaryBasis.GROSS, False,
    )

    assert updated.position == "Администратор"
    assert updated.salary_amount == Decimal("50000")
    assert updated.salary_basis is SalaryBasis.GROSS
    assert updated.active is False
    assert db.execute(
        "SELECT status FROM payroll_periods WHERE id = 'p-draft'"
    ).fetchone()["status"] == "draft"
    assert db.execute(
        "SELECT COUNT(*) FROM payroll_snapshots WHERE period_id = 'p-draft'"
    ).fetchone()[0] == 0
    assert db.execute(
        "SELECT status FROM payroll_periods WHERE id = 'p-approved'"
    ).fetchone()["status"] == "approved"
    assert db.execute(
        "SELECT COUNT(*) FROM payroll_snapshots WHERE period_id = 'p-approved'"
    ).fetchone()[0] == 1


def test_update_employee_rejects_non_finite_or_negative_salary(db) -> None:
    create_store(db, "s1", "Сигаево")
    create_employee(db, "e1", "s1", "Иванов Иван")
    for amount in (Decimal("NaN"), Decimal("Infinity"), Decimal("-1")):
        with pytest.raises(ValueError, match="конечным неотрицательным"):
            update_employee(
                db, "e1", "s1", "Иванов Иван", salary_amount=amount,
                salary_basis=SalaryBasis.UNCONFIRMED,
            )



def test_creating_employee_invalidates_calculated_periods(db) -> None:
    from datetime import date

    from symbol_payroll.infrastructure.payroll_repository import create_period, persist_payroll_result
    from symbol_payroll.domain.calculation import PayrollResult

    create_store(db, "s1", "Сигаево")
    create_period(db, "p1", date(2026, 10, 1), date(2026, 10, 31))
    empty_result = PayrollResult((), Decimal("0"), Decimal("0"), Decimal("0"), Decimal("0"), ())
    persist_payroll_result(db, "p1", empty_result, [])
    assert db.execute(
        "SELECT status FROM payroll_periods WHERE id='p1'"
    ).fetchone()["status"] == "calculated"

    create_employee(db, "e1", "s1", "Иванов Иван")

    assert db.execute(
        "SELECT status FROM payroll_periods WHERE id='p1'"
    ).fetchone()["status"] == "draft"
    assert db.execute(
        "SELECT COUNT(*) FROM payroll_snapshots WHERE period_id='p1'"
    ).fetchone()[0] == 0
