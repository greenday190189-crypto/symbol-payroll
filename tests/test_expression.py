from decimal import Decimal

import pytest

from symbol_payroll.services.expression import ExpressionError, evaluate


def test_arithmetic_expression() -> None:
    assert evaluate("(salary / 30) * shifts", {
        "salary": Decimal("30000"), "shifts": Decimal("15")
    }) == Decimal("15000")


def test_division_by_zero_is_reported() -> None:
    with pytest.raises(ExpressionError, match="Деление на ноль"):
        evaluate("fund / shifts", {"fund": Decimal("100"), "shifts": Decimal("0")})


def test_python_calls_are_rejected() -> None:
    with pytest.raises(ExpressionError, match="Недопустимая конструкция"):
        evaluate("__import__('os')", {})


def test_missing_parameter_is_rejected() -> None:
    with pytest.raises(ExpressionError, match="Неизвестный параметр"):
        evaluate("salary / 30", {})
