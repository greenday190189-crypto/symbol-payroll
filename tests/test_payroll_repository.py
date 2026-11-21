from datetime import date

import pytest

from symbol_payroll.domain.rules import RuleScope
from symbol_payroll.infrastructure.database import connect, initialize
from symbol_payroll.infrastructure.payroll_repository import (
    create_period, list_rule_versions, save_employee_snapshot, save_rule_version,
    transition_period,
)
from symbol_payroll.infrastructure.repositories import create_store, create_employee


@pytest.fixture
def db(tmp_path):
    connection = connect(tmp_path / "payroll.sqlite3")
    initialize(connection)
    create_store(connection, "s1", "Сигаево")
    create_employee(connection, "e1", "s1", "Иванов Иван")
    yield connection
    connection.close()


def test_rule_versions_are_appended_not_overwritten(db) -> None:
    first = save_rule_version(
        db, "base_pay", RuleScope.EMPLOYEE, "e1", "salary / 22 * shifts",
        date(2026, 1, 1),
    )
    second = save_rule_version(
        db, "base_pay", RuleScope.EMPLOYEE, "e1", "salary / 30 * shifts",
        date(2026, 2, 1),
    )
    assert (first.version, second.version) == (1, 2)
    assert len(list_rule_versions(db, "base_pay")) == 2
    with pytest.raises(Exception, match="immutable"):
        db.execute("UPDATE payroll_rules SET expression = '1' WHERE code = 'base_pay'")


def test_approved_snapshots_cannot_be_changed(db) -> None:
    create_period(db, "p1", date(2026, 10, 1), date(2026, 10, 31))
    transition_period(db, "p1", "calculated")
    with pytest.raises(ValueError, match="без сохранённых"):
        transition_period(db, "p1", "approved")
    save_employee_snapshot(
        db, "p1", "e1", {"gross": "42000.00", "net": "36540.00"}, ["base_pay:v1"]
    )
    transition_period(db, "p1", "approved")
    with pytest.raises(Exception, match="immutable"):
        db.execute(
            "UPDATE payroll_snapshots SET snapshot_json = '{}' WHERE period_id = 'p1'"
        )
    with pytest.raises(ValueError, match="Нельзя изменять снимки"):
        save_employee_snapshot(db, "p1", "e1", {"gross": "0"}, [])


def test_period_transition_order_is_enforced(db) -> None:
    create_period(db, "p2", date(2026, 10, 1), date(2026, 10, 31))
    with pytest.raises(ValueError, match="Недопустимый переход"):
        transition_period(db, "p2", "approved")
