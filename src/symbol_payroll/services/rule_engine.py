"""Применение версионируемых правил оплаты к сотрудникам расчётного периода."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal

from symbol_payroll.domain.calculation import PayComponent, money
from symbol_payroll.domain.rules import PayrollRule, RuleScope, select_rule
from symbol_payroll.services.expression import evaluate


@dataclass(frozen=True, slots=True)
class RuleEvaluationInput:
    employee_id: str
    store_id: str
    position: str | None
    period_scope_id: str
    values: dict[str, Decimal] = field(default_factory=dict)


def evaluate_rule_components(
    rules: list[PayrollRule],
    employees: list[RuleEvaluationInput],
    day: date,
    categories: dict[str, str] | None = None,
    titles: dict[str, str] | None = None,
) -> dict[str, list[PayComponent]]:
    """Выбирает наиболее приоритетную версию каждого показателя и вычисляет сумму.

    Периодное исключение адресуется через period_scope_id. Контекст формул
    ограничен переданными Decimal-переменными; произвольный Python запрещён.
    """
    categories = categories or {}
    titles = titles or {}
    known_categories = {"earning", "bonus", "net_salary", "deduction", "unclassified"}
    codes = sorted({rule.code for rule in rules})
    result: dict[str, list[PayComponent]] = {item.employee_id: [] for item in employees}
    if len(result) != len(employees):
        raise ValueError("В контексте правил повторяются идентификаторы сотрудников")

    for employee in employees:
        scope_ids = {
            RuleScope.ORGANIZATION: "*",
            RuleScope.STORE: employee.store_id,
            RuleScope.POSITION: employee.position or "",
            RuleScope.EMPLOYEE: employee.employee_id,
            RuleScope.PERIOD: employee.period_scope_id,
        }
        for code in codes:
            selected = select_rule(
                [rule for rule in rules if rule.code == code], day, scope_ids
            )
            if selected is None:
                continue
            category = categories.get(code, "earning")
            if category not in known_categories:
                raise ValueError(f"Правило {code}: неизвестная категория {category}")
            amount = money(evaluate(selected.expression, employee.values))
            if amount < 0:
                raise ValueError(f"Правило {code}: результат не может быть отрицательным")
            result[employee.employee_id].append(
                PayComponent(code, titles.get(code, code), amount, category)
            )
    return result
