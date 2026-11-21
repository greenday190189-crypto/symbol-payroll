"""SQLite-репозитории справочников магазинов и сотрудников."""

from __future__ import annotations

import sqlite3
from datetime import date, datetime, timezone
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
    hired_on: date | None = None
    salary_effective_from: date | None = None


@dataclass(frozen=True, slots=True)
class SalaryRateRecord:
    id: str
    employee_id: str
    salary_amount: Decimal | None
    salary_basis: SalaryBasis
    effective_from: date | None
    version: int


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


def _insert_salary_rate(
    connection: sqlite3.Connection,
    employee_id: str,
    salary_amount: Decimal | None,
    salary_basis: SalaryBasis,
    effective_from: date | None,
) -> SalaryRateRecord:
    effective_text = effective_from.isoformat() if effective_from else None
    row = connection.execute(
        """SELECT COALESCE(MAX(version), 0) AS version
           FROM employee_salary_history
           WHERE employee_id = ? AND effective_from IS ?""",
        (employee_id, effective_text),
    ).fetchone()
    version = int(row["version"]) + 1
    rate_id = str(uuid.uuid4())
    connection.execute(
        """INSERT INTO employee_salary_history
           (id, employee_id, salary_amount, salary_basis, effective_from, version, created_at)
           VALUES (?, ?, ?, ?, ?, ?, ?)""",
        (
            rate_id, employee_id, str(salary_amount) if salary_amount is not None else None,
            salary_basis.value, effective_text, version,
            datetime.now(timezone.utc).isoformat(),
        ),
    )
    return SalaryRateRecord(
        rate_id, employee_id, salary_amount, salary_basis, effective_from, version
    )


def get_salary_rate_for_date(
    connection: sqlite3.Connection, employee_id: str, effective_on: date
) -> SalaryRateRecord | None:
    """Return the latest salary version effective on a date, or an undated legacy rate."""
    row = connection.execute(
        """SELECT * FROM employee_salary_history
           WHERE employee_id = ? AND effective_from IS NOT NULL AND effective_from <= ?
           ORDER BY effective_from DESC, version DESC LIMIT 1""",
        (employee_id, effective_on.isoformat()),
    ).fetchone()
    if row is None:
        row = connection.execute(
            """SELECT * FROM employee_salary_history
               WHERE employee_id = ? AND effective_from IS NULL
               ORDER BY version DESC LIMIT 1""",
            (employee_id,),
        ).fetchone()
    if row is None:
        return None
    try:
        amount = Decimal(row["salary_amount"]) if row["salary_amount"] is not None else None
        basis = SalaryBasis(row["salary_basis"])
        effective_from = (
            date.fromisoformat(row["effective_from"]) if row["effective_from"] else None
        )
    except (InvalidOperation, ValueError) as exc:
        raise RuntimeError(f"Повреждена история оклада сотрудника {employee_id}") from exc
    return SalaryRateRecord(
        row["id"], row["employee_id"], amount, basis, effective_from, row["version"]
    )


def _latest_salary_rate(
    connection: sqlite3.Connection, employee_id: str
) -> SalaryRateRecord | None:
    row = connection.execute(
        """SELECT * FROM employee_salary_history
           WHERE employee_id = ?
           ORDER BY CASE WHEN effective_from IS NULL THEN 0 ELSE 1 END DESC,
                    effective_from DESC, version DESC
           LIMIT 1""",
        (employee_id,),
    ).fetchone()
    if row is None:
        return None
    try:
        amount = Decimal(row["salary_amount"]) if row["salary_amount"] is not None else None
        basis = SalaryBasis(row["salary_basis"])
        effective_from = (
            date.fromisoformat(row["effective_from"]) if row["effective_from"] else None
        )
    except (InvalidOperation, ValueError) as exc:
        raise RuntimeError(f"Повреждена история оклада сотрудника {employee_id}") from exc
    return SalaryRateRecord(
        row["id"], row["employee_id"], amount, basis, effective_from, row["version"]
    )


def create_employee(
    connection: sqlite3.Connection,
    employee_id: str,
    store_id: str,
    full_name: str,
    position: str | None = None,
    salary_amount: Decimal | None = None,
    salary_basis: SalaryBasis = SalaryBasis.UNCONFIRMED,
    hired_on: date | None = None,
    salary_effective_from: date | None = None,
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
    if not isinstance(salary_basis, SalaryBasis):
        raise TypeError("Тип оклада должен быть значением SalaryBasis")
    if hired_on is not None and not isinstance(hired_on, date):
        raise TypeError("Дата приёма должна быть datetime.date")
    if salary_effective_from is not None and not isinstance(salary_effective_from, date):
        raise TypeError("Дата начала оклада должна быть datetime.date")
    if (
        hired_on is not None and salary_effective_from is not None
        and salary_effective_from < hired_on
    ):
        raise ValueError("Дата начала оклада не может быть раньше даты приёма")
    amount_text = str(salary_amount) if salary_amount is not None else None
    normalized_position = position.strip() if position and position.strip() else None
    with connection:
        connection.execute(
            """INSERT INTO employees
               (id, store_id, full_name, position, salary_amount, salary_basis, hired_on)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (
                employee_id, store_id, name, normalized_position, amount_text,
                salary_basis.value, hired_on.isoformat() if hired_on else None,
            ),
        )
        if salary_amount is not None:
            # Calls from the legacy importer/tests may omit the date. Such a
            # rate is explicitly undated and cannot be approved until confirmed.
            _insert_salary_rate(
                connection, employee_id, salary_amount, salary_basis, salary_effective_from
            )
        # A new employee changes the population of every unapproved payroll.
        invalidate_calculated_periods(connection)
    return EmployeeRecord(
        employee_id, store_id, name, normalized_position, True, salary_amount,
        salary_basis, hired_on, salary_effective_from,
    )


def invalidate_calculated_periods(connection: sqlite3.Connection) -> None:
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
    hired_on: date | None = None,
    salary_effective_from: date | None = None,
) -> EmployeeRecord:
    """Update employee details and append a dated salary-rate version when needed."""
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
    if hired_on is not None and not isinstance(hired_on, date):
        raise TypeError("Дата приёма должна быть datetime.date")
    if salary_effective_from is not None and not isinstance(salary_effective_from, date):
        raise TypeError("Дата начала оклада должна быть datetime.date")
    normalized_position = position.strip() if position and position.strip() else None

    with connection:
        existing = connection.execute(
            "SELECT * FROM employees WHERE id = ?", (employee_id,)
        ).fetchone()
        if existing is None:
            raise LookupError(f"Сотрудник не найден: {employee_id}")
        if active and connection.execute(
            "SELECT 1 FROM termination_cases WHERE employee_id = ?", (employee_id,)
        ).fetchone():
            raise ValueError(
                "Нельзя повторно активировать сотрудника с оформленным увольнением"
            )

        effective_hire_date = hired_on or (
            date.fromisoformat(existing["hired_on"]) if existing["hired_on"] else None
        )
        if (
            effective_hire_date is not None and salary_effective_from is not None
            and salary_effective_from < effective_hire_date
        ):
            raise ValueError("Дата начала оклада не может быть раньше даты приёма")

        old_amount = (
            Decimal(existing["salary_amount"])
            if existing["salary_amount"] is not None else None
        )
        salary_changed = (
            old_amount != salary_amount
            or existing["salary_basis"] != salary_basis.value
        )
        if salary_changed and salary_effective_from is None:
            raise ValueError(
                "При изменении оклада или gross/net укажите дату начала действия новой ставки"
            )

        latest_rate = _latest_salary_rate(connection, employee_id)
        if salary_effective_from is not None:
            matching = connection.execute(
                """SELECT id FROM employee_salary_history
                   WHERE employee_id = ? AND effective_from = ?
                     AND salary_amount IS ? AND salary_basis = ?
                   LIMIT 1""",
                (
                    employee_id, salary_effective_from.isoformat(),
                    str(salary_amount) if salary_amount is not None else None,
                    salary_basis.value,
                ),
            ).fetchone()
            # Also allows an imported undated rate to be explicitly confirmed
            # from a user-supplied date without rewriting the legacy audit row.
            if matching is None:
                _insert_salary_rate(
                    connection, employee_id, salary_amount, salary_basis, salary_effective_from
                )
        elif salary_changed and latest_rate is None and salary_amount is not None:
            raise ValueError("Для нового оклада нужна дата начала действия")

        connection.execute(
            """UPDATE employees
               SET store_id = ?, full_name = ?, position = ?, active = ?,
                   hired_on = COALESCE(?, hired_on)
               WHERE id = ?""",
            (
                store_id, name, normalized_position, int(active),
                effective_hire_date.isoformat() if hired_on is not None else None,
                employee_id,
            ),
        )

        # Keep the compatibility columns aligned with the rate effective today.
        current_rate = get_salary_rate_for_date(connection, employee_id, date.today())
        if current_rate is not None:
            connection.execute(
                "UPDATE employees SET salary_amount = ?, salary_basis = ? WHERE id = ?",
                (
                    str(current_rate.salary_amount) if current_rate.salary_amount is not None else None,
                    current_rate.salary_basis.value, employee_id,
                ),
            )
        elif salary_changed:
            connection.execute(
                "UPDATE employees SET salary_amount = ?, salary_basis = ? WHERE id = ?",
                (
                    str(salary_amount) if salary_amount is not None else None,
                    salary_basis.value, employee_id,
                ),
            )

        changed = (
            existing["store_id"] != store_id
            or existing["full_name"] != name
            or existing["position"] != normalized_position
            or salary_changed
            or existing["salary_basis"] != salary_basis.value
            or bool(existing["active"]) != active
            or (effective_hire_date.isoformat() if effective_hire_date else None) != existing["hired_on"]
        )
        if changed:
            invalidate_calculated_periods(connection)

    return list_employees(connection, include_inactive=True)[
        next(
            index for index, item in enumerate(
                list_employees(connection, include_inactive=True)
            ) if item.id == employee_id
        )
    ]


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
            hired_on = date.fromisoformat(row["hired_on"]) if row["hired_on"] else None
        except (InvalidOperation, ValueError) as exc:
            raise RuntimeError(f"Повреждены данные сотрудника {row['id']}") from exc
        latest = _latest_salary_rate(connection, row["id"])
        records.append(EmployeeRecord(
            row["id"], row["store_id"], row["full_name"], row["position"],
            bool(row["active"]), amount, basis, hired_on,
            latest.effective_from if latest else None,
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
        if active and connection.execute(
            "SELECT 1 FROM termination_cases WHERE employee_id = ?", (employee_id,)
        ).fetchone():
            raise ValueError(
                "Нельзя повторно активировать сотрудника с оформленным увольнением"
            )
        if bool(row["active"]) == active:
            return
        connection.execute(
            "UPDATE employees SET active = ? WHERE id = ?", (int(active), employee_id)
        )
        invalidate_calculated_periods(connection)
