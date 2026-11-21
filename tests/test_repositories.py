from decimal import Decimal

import pytest

from symbol_payroll.domain.pay import SalaryBasis
from symbol_payroll.infrastructure.database import connect, initialize
from symbol_payroll.infrastructure.repositories import (
    create_employee, create_store, list_employees, list_stores, set_employee_active,
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
