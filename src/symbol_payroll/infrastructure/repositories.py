"""SQLite-репозитории справочников магазинов и сотрудников."""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation

from symbol_payroll.domain.pay import SalaryBasis


@dataclass(frozen=True, slots=True)
class StoreRecord:
    id: str
    name: str
    active: bool


@dataclass(frozen=True, slots=True)
class EmployeeRecord:
    id: str
    store_id: str
    full_name: str
    position: str | None
    active: bool
    salary_amount: Decimal | None
    salary_basis: SalaryBasis


def create_store(connection: sqlite3.Connection, store_id: str, name: str) -> StoreRecord:
    normalized = name.strip()
    if not normalized:
        raise ValueError("Название магазина не может быть пустым")
    with connection:
        connection.execute(
            "INSERT INTO stores(id, name) VALUES (?, ?)", (store_id, normalized)
        )
    return StoreRecord(store_id, normalized, True)


def list_stores(connection: sqlite3.Connection, include_inactive: bool = False) -> list[StoreRecord]:
    query = "SELECT id, name, active FROM stores"
    if not include_inactive:
        query += " WHERE active = 1"
    query += " ORDER BY name COLLATE NOCASE"
    return [StoreRecord(row["id"], row["name"], bool(row["active"]))
            for row in connection.execute(query)]


def create_employee(
    connection: sqlite3.Connection,
    employee_id: str,
    store_id: str,
    full_name: str,
    position: str | None = None,
    salary_amount: Decimal | None = None,
    salary_basis: SalaryBasis = SalaryBasis.UNCONFIRMED,
) -> EmployeeRecord:
    name = full_name.strip()
    if not name:
        raise ValueError("ФИО сотрудника не может быть пустым")
    if salary_amount is not None and not isinstance(salary_amount, Decimal):
        raise TypeError("Оклад должен передаваться как Decimal")
    if salary_amount is not None and (
        not salary_amount.is_finite() or salary_amount < 0
    ):
        raise ValueError("Оклад должен быть конечным неотрицательным числом")
    amount_text = str(salary_amount) if salary_amount is not None else None
    normalized_position = position.strip() if position and position.strip() else None
    with connection:
        connection.execute(
            """INSERT INTO employees
               (id, store_id, full_name, position, salary_amount, salary_basis)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (employee_id, store_id, name, normalized_position, amount_text, salary_basis.value),
        )
    return EmployeeRecord(
        employee_id, store_id, name, normalized_position, True, salary_amount, salary_basis
    )


def _invalidate_calculated_periods(connection: sqlite3.Connection) -> None:
    """Return unapproved calculated periods to draft after directory changes."""
    connection.execute(
        """DELETE FROM payroll_snapshots
           WHERE period_id IN (
               SELECT id FROM payroll_periods WHERE status = 'calculated'
           )"""
    )
    connection.execute(
        "UPDATE payroll_periods SET status = 'draft' WHERE status = 'calculated'"
    )


def update_employee(
    connection: sqlite3.Connection,
    employee_id: str,
    store_id: str,
    full_name: str,
    position: str | None = None,
    salary_amount: Decimal | None = None,
    salary_basis: SalaryBasis = SalaryBasis.UNCONFIRMED,
    active: bool = True,
) -> EmployeeRecord:
    """Update a directory record without changing approved payroll history."""
    name = full_name.strip()
    if not name:
        raise ValueError("ФИО сотрудника не может быть пустым")
    if salary_amount is not None and not isinstance(salary_amount, Decimal):
        raise TypeError("Оклад должен передаваться как Decimal")
    if salary_amount is not None and (
        not salary_amount.is_finite() or salary_amount < 0
    ):
        raise ValueError("Оклад должен быть конечным неотрицательным числом")
    if not isinstance(salary_basis, SalaryBasis):
        raise TypeError("Тип оклада должен быть значением SalaryBasis")
    if not isinstance(active, bool):
        raise TypeError("Статус сотрудника должен быть bool")
    normalized_position = position.strip() if position and position.strip() else None
    amount_text = str(salary_amount) if salary_amount is not None else None

    with connection:
        existing = connection.execute(
            "SELECT * FROM employees WHERE id = ?", (employee_id,)
        ).fetchone()
        if existing is None:
            raise LookupError(f"Сотрудник не найден: {employee_id}")

        old_amount = (
            Decimal(existing["salary_amount"])
            if existing["salary_amount"] is not None else None
        )
        changed = (
            existing["store_id"] != store_id
            or existing["full_name"] != name
            or existing["position"] != normalized_position
            or old_amount != salary_amount
            or existing["salary_basis"] != salary_basis.value
            or bool(existing["active"]) != active
        )
        if changed:
            connection.execute(
                """UPDATE employees
                   SET store_id = ?, full_name = ?, position = ?,
                       salary_amount = ?, salary_basis = ?, active = ?
                   WHERE id = ?""",
                (
                    store_id, name, normalized_position, amount_text,
                    salary_basis.value, int(active), employee_id,
                ),
            )
            # Employee data is not effective-dated. Any unapproved calculation
            # may contain stale directory data, so it must be recalculated.
            # Approved and closed snapshots remain immutable and untouched.
            _invalidate_calculated_periods(connection)

    return EmployeeRecord(
        employee_id, store_id, name, normalized_position, active,
        salary_amount, salary_basis,
    )


def list_employees(
    connection: sqlite3.Connection,
    store_id: str | None = None,
    include_inactive: bool = False,
) -> list[EmployeeRecord]:
    clauses: list[str] = []
    params: list[str] = []
    if store_id is not None:
        clauses.append("store_id = ?")
        params.append(store_id)
    if not include_inactive:
        clauses.append("active = 1")
    query = "SELECT * FROM employees"
    if clauses:
        query += " WHERE " + " AND ".join(clauses)
    query += " ORDER BY full_name COLLATE NOCASE"
    records = []
    for row in connection.execute(query, params):
        try:
            amount = Decimal(row["salary_amount"]) if row["salary_amount"] is not None else None
            basis = SalaryBasis(row["salary_basis"])
        except (InvalidOperation, ValueError) as exc:
            raise RuntimeError(f"Повреждены данные сотрудника {row['id']}") from exc
        records.append(EmployeeRecord(
            row["id"], row["store_id"], row["full_name"], row["position"],
            bool(row["active"]), amount, basis,
        ))
    return records


def set_employee_active(
    connection: sqlite3.Connection, employee_id: str, active: bool
) -> None:
    if not isinstance(active, bool):
        raise TypeError("Статус сотрудника должен быть bool")
    with connection:
        row = connection.execute(
            "SELECT active FROM employees WHERE id = ?", (employee_id,)
        ).fetchone()
        if row is None:
            raise LookupError(f"Сотрудник не найден: {employee_id}")
        if bool(row["active"]) == active:
            return
        connection.execute(
            "UPDATE employees SET active = ? WHERE id = ?", (int(active), employee_id)
        )
        _invalidate_calculated_periods(connection)
