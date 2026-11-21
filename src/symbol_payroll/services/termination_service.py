"""Отдельный сценарий окончательного расчёта при увольнении."""

from __future__ import annotations

import json
import sqlite3
from datetime import date, datetime, timezone
from decimal import Decimal
from uuid import uuid4

from symbol_payroll.domain.calculation import EmployeeInput, PayComponent, calculate_payroll
from symbol_payroll.domain.pay import SalaryBasis
from symbol_payroll.domain.tax import TaxMode
from symbol_payroll.infrastructure.repositories import invalidate_calculated_periods

ZERO = Decimal("0")
ALLOWED_CATEGORIES = {"earning", "bonus", "net_salary", "deduction", "unclassified"}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _decimal(value, label: str) -> Decimal:
    try:
        amount = Decimal(str(value).replace(",", "."))
    except Exception as exc:
        raise ValueError(f"Некорректное значение: {label}") from exc
    if not amount.is_finite() or amount < ZERO:
        raise ValueError(f"{label}: сумма должна быть конечной и неотрицательной")
    return amount


def _normalize_components(components: list[dict]) -> list[dict]:
    if not isinstance(components, list):
        raise ValueError("Список окончательных начислений должен быть массивом")
    normalized = []
    for index, item in enumerate(components, start=1):
        if not isinstance(item, dict):
            raise ValueError(f"Строка начисления {index} имеет неверный формат")
        code = str(item.get("code", "")).strip()
        title = str(item.get("title", "")).strip()
        category = str(item.get("category", "earning")).strip()
        if not code or not title:
            raise ValueError(f"В строке начисления {index} нужны код и название")
        if category not in ALLOWED_CATEGORIES:
            raise ValueError(f"Недопустимая категория строки {title}: {category}")
        amount = _decimal(item.get("amount", 0), title)
        normalized.append({
            "code": code, "title": title, "category": category, "amount": str(amount),
        })
    return normalized


def create_termination_case(
    connection: sqlite3.Connection,
    employee_id: str,
    termination_date: date,
    *,
    worked_shifts: Decimal = ZERO,
    worked_hours: Decimal = ZERO,
    advances: Decimal = ZERO,
    other_deductions: Decimal = ZERO,
    manual_tax: Decimal = ZERO,
    components: list[dict] | None = None,
    notes: str = "",
) -> str:
    """Создаёт дело об увольнении и исключает сотрудника из будущих периодов."""
    amounts = {
        "worked_shifts": _decimal(worked_shifts, "Отработанные смены"),
        "worked_hours": _decimal(worked_hours, "Отработанные часы"),
        "advances": _decimal(advances, "Авансы"),
        "other_deductions": _decimal(other_deductions, "Прочие удержания"),
        "manual_tax": _decimal(manual_tax, "Ручной НДФЛ"),
    }
    normalized = _normalize_components(components or [])
    employee = connection.execute(
        "SELECT id, active FROM employees WHERE id = ?", (employee_id,)
    ).fetchone()
    if employee is None:
        raise LookupError(f"Сотрудник не найден: {employee_id}")
    if not employee["active"]:
        raise ValueError("Нельзя оформить увольнение: сотрудник уже неактивен")
    case_id = str(uuid4())
    now = _now()
    with connection:
        connection.execute(
            """INSERT INTO termination_cases (
                id, employee_id, termination_date, worked_shifts, worked_hours,
                advances, other_deductions, manual_tax, components_json, notes,
                status, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'draft', ?, ?)""",
            (
                case_id, employee_id, termination_date.isoformat(),
                str(amounts["worked_shifts"]), str(amounts["worked_hours"]),
                str(amounts["advances"]), str(amounts["other_deductions"]),
                str(amounts["manual_tax"]),
                json.dumps(normalized, ensure_ascii=False, sort_keys=True),
                notes.strip(), now, now,
            ),
        )
        connection.execute("UPDATE employees SET active = 0 WHERE id = ?", (employee_id,))
        invalidate_calculated_periods(connection)
    return case_id


def list_termination_cases(connection: sqlite3.Connection) -> list[dict]:
    rows = connection.execute(
        """SELECT tc.*, e.full_name, e.store_id, s.name AS store_name
           FROM termination_cases tc
           JOIN employees e ON e.id = tc.employee_id
           JOIN stores s ON s.id = e.store_id
           ORDER BY tc.termination_date DESC, e.full_name COLLATE NOCASE"""
    )
    return [dict(row) for row in rows]


def calculate_termination_case(connection: sqlite3.Connection, case_id: str):
    """Рассчитывает только введённые вручную компоненты; отпускная компенсация не выводится."""
    case = connection.execute(
        """SELECT tc.*, e.full_name FROM termination_cases tc
           JOIN employees e ON e.id = tc.employee_id WHERE tc.id = ?""",
        (case_id,),
    ).fetchone()
    if case is None:
        raise LookupError(f"Дело об увольнении не найдено: {case_id}")
    if case["status"] == "approved":
        raise ValueError("Утверждённый окончательный расчёт нельзя пересчитывать")
    components = json.loads(case["components_json"])
    lines = [
        PayComponent(
            item["code"], item["title"], Decimal(item["amount"]), item["category"]
        )
        for item in components
    ]
    if Decimal(case["advances"]) > ZERO:
        lines.append(PayComponent(
            "termination_advances", "Ранее выплаченные авансы",
            Decimal(case["advances"]), "deduction",
        ))
    if Decimal(case["other_deductions"]) > ZERO:
        lines.append(PayComponent(
            "termination_deductions", "Прочие удержания",
            Decimal(case["other_deductions"]), "deduction",
        ))
    employee = EmployeeInput(
        employee_id=case["employee_id"],
        full_name=case["full_name"],
        salary_amount=None,
        salary_basis=SalaryBasis.GROSS,
        shifts=Decimal(case["worked_shifts"]),
        hours=Decimal(case["worked_hours"]),
        manual_tax=Decimal(case["manual_tax"]),
    )
    result = calculate_payroll(
        [employee], components={case["employee_id"]: lines}, tax_mode=TaxMode.MANUAL
    )
    employee_result = result.employees[0]
    payload = {
        "case_id": case_id,
        "employee_id": employee_result.employee_id,
        "full_name": employee_result.full_name,
        "termination_date": case["termination_date"],
        "worked_shifts": case["worked_shifts"],
        "worked_hours": case["worked_hours"],
        "components": [
            {
                "code": item.code, "title": item.title, "amount": str(item.amount),
                "category": item.category,
            }
            for item in employee_result.components
        ],
        "gross_total": str(employee_result.gross_total),
        "tax_amount": str(employee_result.tax_amount),
        "deduction_total": str(employee_result.deduction_total),
        "net_total": str(employee_result.net_total),
        "warnings": list(result.warnings),
        "calculation_note": (
            "Расчёт построен только из введённых компонентов. Полный оклад и "
            "компенсация неиспользованного отпуска автоматически не начислялись."
        ),
    }
    with connection:
        connection.execute(
            """UPDATE termination_cases SET result_json = ?, status = 'calculated',
               updated_at = ? WHERE id = ?""",
            (json.dumps(payload, ensure_ascii=False, sort_keys=True), _now(), case_id),
        )
    return payload


def approve_termination_case(connection: sqlite3.Connection, case_id: str) -> None:
    with connection:
        case = connection.execute(
            "SELECT status, result_json FROM termination_cases WHERE id = ?",
            (case_id,),
        ).fetchone()
        if case is None:
            raise LookupError(f"Дело об увольнении не найдено: {case_id}")
        if case["status"] != "calculated" or not case["result_json"]:
            raise ValueError("Сначала выполните и сохраните окончательный расчёт")
        connection.execute(
            "UPDATE termination_cases SET status = 'approved', approved_at = ?, updated_at = ? WHERE id = ?",
            (_now(), _now(), case_id),
        )
