"""Хранение вводных показателей сотрудника за расчётный период."""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from typing import Mapping


ZERO = Decimal("0")


@dataclass(frozen=True, slots=True)
class PeriodEmployeeInput:
    period_id: str
    employee_id: str
    shifts: Decimal = ZERO
    hours: Decimal = ZERO
    sales: Decimal = ZERO
    plan: Decimal = ZERO
    advances: Decimal = ZERO
    deductions: Decimal = ZERO
    manual_tax: Decimal = ZERO
    fixed_adjustments: Decimal = ZERO
    extra_values: Mapping[str, Decimal] = field(default_factory=dict)

    def validate(self) -> None:
        if not self.period_id or not self.employee_id:
            raise ValueError("Не заданы период или сотрудник")
        for key in (
            "shifts", "hours", "sales", "plan", "advances", "deductions",
            "manual_tax", "fixed_adjustments",
        ):
            value = getattr(self, key)
            if not value.is_finite() or value < ZERO:
                raise ValueError(f"Показатель {key} должен быть конечным и неотрицательным")
        for key, value in self.extra_values.items():
            if not key.isidentifier():
                raise ValueError(f"Некорректное имя дополнительного показателя: {key}")
            if not value.is_finite():
                raise ValueError(f"Дополнительный показатель {key} должен быть конечным")


def save_period_input(
    connection: sqlite3.Connection, item: PeriodEmployeeInput
) -> None:
    item.validate()
    with connection:
        period = connection.execute(
            "SELECT status FROM payroll_periods WHERE id = ?", (item.period_id,)
        ).fetchone()
        if period is None:
            raise LookupError(f"Расчётный период не найден: {item.period_id}")
        if period["status"] not in ("draft", "calculated"):
            raise ValueError("Нельзя изменять вводные данные утверждённого периода")
        employee = connection.execute(
            "SELECT id FROM employees WHERE id = ?", (item.employee_id,)
        ).fetchone()
        if employee is None:
            raise LookupError(f"Сотрудник не найден: {item.employee_id}")
        extra = json.dumps(
            {key: str(value) for key, value in item.extra_values.items()},
            ensure_ascii=False, sort_keys=True,
        )
        connection.execute(
            """INSERT INTO payroll_inputs (
                period_id, employee_id, shifts, hours, sales, plan, advances,
                deductions, manual_tax, fixed_adjustments, extra_values_json, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(period_id, employee_id) DO UPDATE SET
                shifts=excluded.shifts, hours=excluded.hours, sales=excluded.sales,
                plan=excluded.plan, advances=excluded.advances,
                deductions=excluded.deductions, manual_tax=excluded.manual_tax,
                fixed_adjustments=excluded.fixed_adjustments,
                extra_values_json=excluded.extra_values_json, updated_at=excluded.updated_at""",
            (
                item.period_id, item.employee_id, str(item.shifts), str(item.hours),
                str(item.sales), str(item.plan), str(item.advances), str(item.deductions),
                str(item.manual_tax), str(item.fixed_adjustments), extra,
                datetime.now(timezone.utc).isoformat(),
            ),
        )


def list_period_inputs(
    connection: sqlite3.Connection, period_id: str
) -> dict[str, PeriodEmployeeInput]:
    result = {}
    rows = connection.execute(
        "SELECT * FROM payroll_inputs WHERE period_id = ? ORDER BY employee_id",
        (period_id,),
    )
    for row in rows:
        try:
            extra = {
                key: Decimal(value)
                for key, value in json.loads(row["extra_values_json"]).items()
            }
            item = PeriodEmployeeInput(
                row["period_id"], row["employee_id"],
                Decimal(row["shifts"]), Decimal(row["hours"]),
                Decimal(row["sales"]), Decimal(row["plan"]),
                Decimal(row["advances"]), Decimal(row["deductions"]),
                Decimal(row["manual_tax"]), Decimal(row["fixed_adjustments"]), extra,
            )
            item.validate()
        except (InvalidOperation, ValueError, TypeError) as exc:
            raise RuntimeError(
                f"Повреждены вводные данные сотрудника {row['employee_id']}"
            ) from exc
        result[item.employee_id] = item
    return result
