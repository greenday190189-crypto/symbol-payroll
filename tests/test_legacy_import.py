from __future__ import annotations

import sqlite3

import pytest

from symbol_payroll.infrastructure.database import initialize
from symbol_payroll.infrastructure.legacy_import import import_legacy_directory


def test_legacy_directory_imports_stores_and_employees_with_unconfirmed_salary_basis():
    connection = sqlite3.connect(":memory:")
    connection.row_factory = sqlite3.Row
    initialize(connection)

    result = import_legacy_directory(connection)

    assert result == {"stores": 8, "employees": 43, "role_placeholders": 12}
    assert connection.execute("SELECT COUNT(*) FROM stores").fetchone()[0] == 8
    assert connection.execute("SELECT COUNT(*) FROM employees").fetchone()[0] == 43
    assert connection.execute(
        "SELECT COUNT(*) FROM employees WHERE salary_basis != 'unconfirmed'"
    ).fetchone()[0] == 0
    assert connection.execute(
        "SELECT COUNT(*) FROM employees WHERE salary_amount IS NOT NULL"
    ).fetchone()[0] == 43


def test_legacy_import_refuses_non_empty_directory_without_partial_writes():
    connection = sqlite3.connect(":memory:")
    connection.row_factory = sqlite3.Row
    initialize(connection)
    connection.execute("INSERT INTO stores(id, name) VALUES ('existing', 'Existing')")

    with pytest.raises(ValueError, match="только в пустой справочник"):
        import_legacy_directory(connection)

    assert connection.execute("SELECT COUNT(*) FROM stores").fetchone()[0] == 1
    assert connection.execute("SELECT COUNT(*) FROM employees").fetchone()[0] == 0
