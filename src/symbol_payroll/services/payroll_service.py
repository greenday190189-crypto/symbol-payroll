"""Сервис полного расчёта расчётного периода."""

from __future__ import annotations

import sqlite3
from datetime import date
from decimal import Decimal

from symbol_payroll.domain.calculation import (
    EmployeeInput, PayComponent, calculate_payroll,
)
from symbol_payroll.domain.pay import SalaryBasis
from symbol_payroll.domain.tax import TaxMode
from symbol_payroll.infrastructure.period_inputs import (
    PeriodEmployeeInput, list_period_inputs,
)
from symbol_payroll.infrastructure.payroll_repository import (
    list_rule_versions, persist_payroll_result,
)
from symbol_payroll.infrastructure.repositories import get_salary_rate_for_date, list_employees
from symbol_payroll.services.rule_engine import (
    RuleEvaluationInput, evaluate_rule_components,
)


ZERO = Decimal("0")


def calculate_period(
    connection: sqlite3.Connection,
    period_id: str,
    tax_mode: TaxMode = TaxMode.DISABLED,
):
    """Calculate a monthly payroll using the salary version effective at period start."""
    period = connection.execute(
        "SELECT period_start, period_end, status FROM payroll_periods WHERE id = ?",
        (period_id,),
    ).fetchone()
    if period is None:
        raise LookupError(f"Расчётный период не найден: {period_id}")
    if period["status"] not in ("draft", "calculated"):
        raise ValueError("Можно рассчитывать только черновой или рассчитанный период")

    period_start = date.fromisoformat(period["period_start"])
    period_end = date.fromisoformat(period["period_end"])
    calculation_day = period_start
    all_people = list_employees(connection, include_inactive=True)
    terminations = {
        row["employee_id"]: date.fromisoformat(row["termination_date"])
        for row in connection.execute(
            "SELECT employee_id, termination_date FROM termination_cases"
        )
    }

    employees = []
    for person in all_people:
        if person.hired_on is not None and person.hired_on > period_end:
            continue
        termination_date = terminations.get(person.id)
        if termination_date is not None:
            if termination_date < period_start:
                continue
            employees.append(person)
        elif person.active:
            employees.append(person)

    if not employees:
        raise ValueError("Нет сотрудников, подлежащих расчёту за выбранный период")

    inputs = list_period_inputs(connection, period_id)
    rules = list_rule_versions(connection)
    employee_contexts = []
    employee_inputs = []
    components_by_employee: dict[str, list[PayComponent]] = {}
    employee_metadata: dict[str, dict] = {}
    warnings = []

    for person in employees:
        rate = get_salary_rate_for_date(connection, person.id, calculation_day)
        has_salary_history = connection.execute(
            "SELECT 1 FROM employee_salary_history WHERE employee_id = ? LIMIT 1",
            (person.id,),
        ).fetchone() is not None
        salary_amount = (
            rate.salary_amount if rate else (
                None if has_salary_history else person.salary_amount
            )
        )
        salary_basis = rate.salary_basis if rate else person.salary_basis
        salary_effective_from = rate.effective_from if rate else None
        salary_version = rate.version if rate else None

        if person.hired_on is None:
            warnings.append(
                f"{person.full_name}: дата приёма не подтверждена; "
                "нельзя достоверно определить право участия в этом периоде"
            )
        hired_during_period = (
            person.hired_on is not None
            and period_start < person.hired_on <= period_end
        )
        if hired_during_period:
            warnings.append(
                f"{person.full_name}: сотрудник принят в течение периода; "
                "полный месячный оклад автоматически не начислен"
            )

        termination_date = terminations.get(person.id)
        terminated_during_period = (
            termination_date is not None
            and period_start <= termination_date <= period_end
        )
        if terminated_during_period:
            warnings.append(
                f"{person.full_name}: увольнение внутри периода; полный месячный оклад "
                "автоматически не начислен. Настройте формулу оплаты фактического времени."
            )

        if salary_amount is not None and salary_effective_from is None and rate is not None:
            warnings.append(
                f"{person.full_name}: дата начала действия оклада не подтверждена; "
                "ставка перенесена из старой карточки без даты"
            )

        future_salary_date = connection.execute(
            """SELECT MIN(effective_from) AS next_effective_from
               FROM employee_salary_history
               WHERE employee_id = ? AND effective_from > ?""",
            (person.id, period_end.isoformat()),
        ).fetchone()["next_effective_from"]
        if rate is None and has_salary_history and future_salary_date:
            warnings.append(
                f"{person.full_name}: нет ставки оклада, действующей в расчётном периоде; "
                f"первая подтверждённая ставка начинается {future_salary_date}"
            )

        salary_change = connection.execute(
            """SELECT MIN(effective_from) AS next_effective_from
               FROM employee_salary_history
               WHERE employee_id = ? AND effective_from > ? AND effective_from <= ?""",
            (person.id, period_start.isoformat(), period_end.isoformat()),
        ).fetchone()["next_effective_from"]
        salary_changes_during_period = salary_change is not None
        if salary_changes_during_period:
            warnings.append(
                f"{person.full_name}: новая ставка начинает действовать "
                f"{salary_change} внутри периода; требуется отдельный расчёт по отрезкам"
            )

        period_input = inputs.get(person.id)
        if period_input is None:
            period_input = PeriodEmployeeInput(period_id, person.id)
            warnings.append(
                f"{person.full_name}: вводные показатели за период не заполнены; "
                "смены, часы, продажи и удержания приняты равными нулю"
            )

        salary = salary_amount or ZERO
        gross_salary = salary if salary_basis is SalaryBasis.GROSS else ZERO
        net_salary = salary if salary_basis is SalaryBasis.NET else ZERO
        values = {
            "salary": gross_salary,
            "salary_net": net_salary,
            "shifts": period_input.shifts,
            "hours": period_input.hours,
            "sales": period_input.sales,
            "plan": period_input.plan,
            "advances": period_input.advances,
            "deductions": period_input.deductions,
            "manual_tax": period_input.manual_tax,
            "fixed_adjustments": period_input.fixed_adjustments,
        }
        values.update(period_input.extra_values)
        employee_contexts.append(
            RuleEvaluationInput(
                person.id, person.store_id, person.position,
                f"{period_id}:{person.id}", values,
            )
        )

        # Do not auto-add a full monthly rate when the employment or rate
        # boundary is unknown or falls inside the month. A configured formula
        # may still use the salary variable and actual shifts/hours, but approval
        # remains blocked until the monthly split is resolved.
        unsafe_monthly_base = (
            person.hired_on is None
            or hired_during_period
            or terminated_during_period
            or salary_effective_from is None
            or salary_changes_during_period
        )
        if salary_basis is SalaryBasis.UNCONFIRMED:
            # Preserve the visible but non-payable amount for manual review.
            base_salary_amount = salary_amount
        else:
            base_salary_amount = None if unsafe_monthly_base else salary_amount

        employee_inputs.append(
            EmployeeInput(
                person.id, person.full_name, base_salary_amount,
                salary_basis, period_input.shifts, period_input.hours,
                fixed_adjustments=period_input.fixed_adjustments,
                manual_tax=period_input.manual_tax,
            )
        )
        components_by_employee[person.id] = []
        if period_input.advances:
            components_by_employee[person.id].append(
                PayComponent("advance", "Аванс", period_input.advances, "deduction")
            )
        if period_input.deductions:
            components_by_employee[person.id].append(
                PayComponent(
                    "manual_deduction", "Прочие удержания",
                    period_input.deductions, "deduction",
                )
            )

        employee_metadata[person.id] = {
            "hired_on": person.hired_on.isoformat() if person.hired_on else None,
            "termination_date": termination_date.isoformat() if termination_date else None,
            "salary_amount": str(salary_amount) if salary_amount is not None else None,
            "salary_basis": salary_basis.value,
            "salary_effective_from": (
                salary_effective_from.isoformat() if salary_effective_from else None
            ),
            "salary_version": salary_version,
            "salary_date_confirmed": salary_effective_from is not None,
            "monthly_base_applied": (
                str(base_salary_amount) if base_salary_amount is not None else None
            ),
        }

    rule_components = evaluate_rule_components(rules, employee_contexts, calculation_day)
    for employee_id, lines in rule_components.items():
        components_by_employee[employee_id].extend(lines)

    result = calculate_payroll(
        employee_inputs,
        components=components_by_employee,
        tax_mode=tax_mode,
    )
    if warnings:
        from dataclasses import replace
        result = replace(result, warnings=tuple((*result.warnings, *warnings)))

    rule_versions = [
        f"{rule.code}:{rule.scope.value}:{rule.scope_id}:v{rule.version}:"
        f"{rule.effective_from.isoformat()}:{rule.effective_to or ''}:"
        f"{rule.category}:{rule.title}:{rule.expression}"
        for rule in rules
    ]
    persist_payroll_result(
        connection, period_id, result, rule_versions,
        employee_metadata=employee_metadata,
    )
    return result
