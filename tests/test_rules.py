from datetime import date

import pytest

from symbol_payroll.domain.rules import PayrollRule, RuleScope, select_rule


def rule(scope: RuleScope, scope_id: str, expression: str) -> PayrollRule:
    return PayrollRule("base_pay", scope, scope_id, expression, 1, date(2026, 1, 1))


def test_employee_rule_overrides_store_rule() -> None:
    selected = select_rule([
        rule(RuleScope.STORE, "store-1", "store_rate * shifts"),
        rule(RuleScope.EMPLOYEE, "employee-1", "salary / 22 * shifts"),
    ], date(2026, 10, 1))
    assert selected is not None
    assert selected.scope is RuleScope.EMPLOYEE


def test_ambiguous_rules_at_same_scope_are_rejected() -> None:
    with pytest.raises(ValueError, match="Неоднозначные"):
        select_rule([
            rule(RuleScope.EMPLOYEE, "employee-1", "salary / 22"),
            rule(RuleScope.EMPLOYEE, "employee-2", "salary / 30"),
        ], date(2026, 10, 1))
