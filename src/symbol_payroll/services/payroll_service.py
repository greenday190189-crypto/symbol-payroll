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
from symbol_payroll.infrastructure.repositories import list_employees
from symbol_payroll.services.rule_engine import (
    RuleEvaluationInput, evaluate_rule_components,
)


ZERO = Decimal("0")


def calculate_period(
    connection: sqlite3.Connection,
    period_id: str,
    tax_mode: TaxMode = TaxMode.DISABLED,
):
    """Рассчитывает всех активных сотрудников и атомарно сохраняет результат.

    Отсутствующие вводные строки трактуются как нулевые операционные показатели,
    но фиксируются предупреждением. Оклад с неопределённой базой не передаётся
    в переменную salary формул, чтобы правило не превратило его в gross.
    """
    period = connection.execute(
        "SELECT period_start, period_end, status FROM payroll_periods WHERE id = ?",
        (period_id,),
    ).fetchone()
    if period is None:
        raise LookupError(f"Расчётный период не найден: {period_id}")
    if period["status"] not in ("draft", "calculated"):
        raise ValueError("Можно рассчитывать только черновой или рассчитанный период")
    period_start = date.fromisoformat(period["period_start"])
    calculation_day = period_start
    # Сотрудник с оформленным увольнением остаётся в ведомостях периодов,
    # которые начались не позднее даты увольнения. В более поздние периоды он не попадает.
    all_people = list_employees(connection, include_inactive=True)
    terminations = {
        row["employee_id"]: date.fromisoformat(row["termination_date"])
        for row in connection.execute(
            "SELECT employee_id, termination_date FROM termination_cases"
        )
    }
    employees = []
    for person in all_people:
        termination_date = terminations.get(person.id)
        if termination_date is not None:
            # A recorded termination date always wins over the active flag:
            # include historical periods up to that date, never later periods.
            if termination_date >= period_start:
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
    warnings = []

    for person in employees:
        period_input = inputs.get(person.id)
        if period_input is None:
            period_input = PeriodEmployeeInput(period_id, person.id)
            warnings.append(
                f"{person.full_name}: вводные показатели за период не заполнены; "
                "смены, часы, продажи и удержания приняты равными нулю"
            )
        salary = person.salary_amount or ZERO
        gross_salary = salary if person.salary_basis is SalaryBasis.GROSS else ZERO
        net_salary = salary if person.salary_basis is SalaryBasis.NET else ZERO
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
        termination_date = terminations.get(person.id)
        terminated_during_period = (
            termination_date is not None
            and date.fromisoformat(period["period_start"]) <= termination_date
            <= date.fromisoformat(period["period_end"])
        )
        salary_amount = None if terminated_during_period else person.salary_amount
        if terminated_during_period:
            warnings.append(
                f"{person.full_name}: увольнение внутри периода; полный месячный оклад "
                "автоматически не начислен. Настройте формулу оплаты фактического времени."
            )
        employee_inputs.append(
            EmployeeInput(
                person.id, person.full_name, salary_amount,
                person.salary_basis, period_input.shifts, period_input.hours,
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
                PayComponent("manual_deduction", "Прочие удержания",
                             period_input.deductions, "deduction")
            )

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

    # Fingerprint contains the complete immutable rule set known at calculation time.
    rule_versions = [
        f"{rule.code}:{rule.scope.value}:{rule.scope_id}:v{rule.version}:"
        f"{rule.effective_from.isoformat()}:{rule.effective_to or ''}:"
        f"{rule.category}:{rule.title}:{rule.expression}"
        for rule in rules
    ]
    persist_payroll_result(connection, period_id, result, rule_versions)
    return result
