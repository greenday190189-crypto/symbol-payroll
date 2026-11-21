"""One-time import of the legacy workbook directory into an empty local database."""

from __future__ import annotations

import sqlite3
import uuid
from datetime import datetime, timezone

from symbol_payroll.legacy_directory_data import LEGACY_DIRECTORY


def import_legacy_directory(connection: sqlite3.Connection) -> dict[str, int]:
    """Import source stores and named employees once, preserving unknown salary basis."""
    counts = connection.execute(
        "SELECT (SELECT COUNT(*) FROM stores), (SELECT COUNT(*) FROM employees)"
    ).fetchone()
    if counts[0] or counts[1]:
        raise ValueError("Импорт исходной ведомости разрешён только в пустой справочник")
    store_ids = {
        name: str(uuid.uuid5(uuid.NAMESPACE_URL, "symbol-payroll:store:" + name.casefold()))
        for name in LEGACY_DIRECTORY["stores"]
    }
    with connection:
        for name, store_id in store_ids.items():
            connection.execute(
                "INSERT INTO stores(id, name, active) VALUES (?, ?, 1)", (store_id, name)
            )
        for store, full_name, salary in LEGACY_DIRECTORY["employees"]:
            employee_id = str(uuid.uuid5(
                uuid.NAMESPACE_URL, f"symbol-payroll:{store}:{full_name.casefold()}"
            ))
            connection.execute(
                """INSERT INTO employees
                   (id, store_id, full_name, position, active, salary_amount, salary_basis)
                   VALUES (?, ?, ?, NULL, 1, ?, 'unconfirmed')""",
                (employee_id, store_ids[store], full_name, salary),
            )
            history_id = str(uuid.uuid5(
                uuid.NAMESPACE_URL, f"symbol-payroll:legacy-salary:{employee_id}"
            ))
            connection.execute(
                """INSERT INTO employee_salary_history
                   (id, employee_id, salary_amount, salary_basis, effective_from, version, created_at)
                   VALUES (?, ?, ?, 'unconfirmed', NULL, 1, ?)""",
                (history_id, employee_id, salary, datetime.now(timezone.utc).isoformat()),
            )
    return {
        "stores": len(LEGACY_DIRECTORY["stores"]),
        "employees": len(LEGACY_DIRECTORY["employees"]),
        "role_placeholders": len(LEGACY_DIRECTORY["role_placeholders"]),
    }
