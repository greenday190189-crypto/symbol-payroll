from datetime import date
from decimal import Decimal

import pytest

from symbol_payroll.domain.rules import PayrollRule, RuleScope
from symbol_payroll.services.rule_engine import RuleEvaluationInput, evaluate_rule_components


def test_rule_engine_applies_employee_override_and_evaluates_formula() -> None:
    rules = [
        PayrollRule(
            "shift_pay", RuleScope.STORE, "store-1", "store_rate * shifts",
            1, date(2026, 1, 1),
        ),
        PayrollRule(
            "shift_pay", RuleScope.EMPLOYEE, "employee-1", "salary / 30 * shifts",
            1, date(2026, 1, 1),
        ),
    ]
    context = RuleEvaluationInput(
        "employee-1", "store-1", "cashier", "period-1:employee-1",
        {"salary": Decimal("30000"), "shifts": Decimal("15"), "store_rate": Decimal("1000")},
    )
    result = evaluate_rule_components(rules, [context], date(2026, 10, 1))
    assert result["employee-1"][0].amount == Decimal("15000.00")


def test_rule_engine_supports_period_override_and_deduction_category() -> None:
    rules = [
        PayrollRule(
            "advance", RuleScope.PERIOD, "period-1:employee-1", "advance_amount",
            1, date(2026, 10, 1),
        )
    ]
    context = RuleEvaluationInput(
        "employee-1", "store-1", None, "period-1:employee-1",
        {"advance_amount": Decimal("5000")},
    )
    result = evaluate_rule_components(
        rules, [context], date(2026, 10, 15), categories={"advance": "deduction"},
        titles={"advance": "Аванс"},
    )
    assert result["employee-1"][0].category == "deduction"
    assert result["employee-1"][0].amount == Decimal("5000.00")


def test_rule_engine_reports_missing_variables() -> None:
    rule = PayrollRule(
        "pay", RuleScope.ORGANIZATION, "*", "salary / shifts",
        1, date(2026, 1, 1),
    )
    context = RuleEvaluationInput("e1", "s1", None, "p1:e1", {"salary": Decimal("100")})
    with pytest.raises(ValueError, match="Неизвестный параметр"):
        evaluate_rule_components([rule], [context], date(2026, 10, 1))


def test_rule_engine_rejects_negative_result() -> None:
    rule = PayrollRule(
        "pay", RuleScope.EMPLOYEE, "e1", "-1",
        1, date(2026, 1, 1),
    )
    context = RuleEvaluationInput("e1", "s1", None, "p1:e1", {})
    with pytest.raises(ValueError, match="не может быть отрицательным"):
        evaluate_rule_components([rule], [context], date(2026, 10, 1))
