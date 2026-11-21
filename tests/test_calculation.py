from decimal import Decimal

import pytest

from symbol_payroll.domain.calculation import (
    AllocationMethod, BonusPool, EmployeeInput, PayComponent, calculate_payroll,
)
from symbol_payroll.domain.pay import SalaryBasis
from symbol_payroll.domain.tax import TaxMode


def employee(person_id: str, shifts: str = "1", hours: str = "8") -> EmployeeInput:
    return EmployeeInput(
        person_id, person_id, Decimal("30000"), SalaryBasis.GROSS,
        Decimal(shifts), Decimal(hours),
    )


def test_individual_components_and_manual_tax() -> None:
    result = calculate_payroll(
        [employee("a")],
        components={"a": [PayComponent("sales", "Оплата смен", Decimal("12000"))]},
        tax_mode=TaxMode.MANUAL,
    )
    item = result.employees[0]
    assert item.gross_total == Decimal("42000.00")
    assert item.net_total == Decimal("42000.00")


def test_bonus_pool_proportional_to_shifts() -> None:
    pool = BonusPool(
        "sales_bonus", "Премия", Decimal("1000"), ("a", "b"), ("a", "b"),
        AllocationMethod.SHIFTS,
    )
    result = calculate_payroll([employee("a", "1"), employee("b", "3")], bonus_pools=[pool])
    amounts = {item.employee_id: item.components[-1].amount for item in result.employees}
    assert amounts == {"a": Decimal("250.00"), "b": Decimal("750.00")}


def test_bonus_participants_can_differ_from_recipients() -> None:
    pool = BonusPool(
        "pool", "Премия", Decimal("900"), ("a", "b", "c"), ("a", "b"),
        AllocationMethod.EQUAL,
    )
    result = calculate_payroll(
        [employee("a"), employee("b"), employee("c")], bonus_pools=[pool]
    )
    amounts = {
        item.employee_id: sum((line.amount for line in item.components if line.code == "pool"), Decimal("0"))
        for item in result.employees
    }
    assert amounts == {"a": Decimal("300.00"), "b": Decimal("300.00"), "c": Decimal("0")}


def test_subset_of_recipients_does_not_redistribute_nonrecipients_share() -> None:
    pool = BonusPool(
        "pool", "Премия", Decimal("1200"), ("a", "b", "c", "d"),
        ("a", "b"), AllocationMethod.EQUAL,
    )
    result = calculate_payroll(
        [employee("a"), employee("b"), employee("c"), employee("d")],
        bonus_pools=[pool],
    )
    paid = sum(
        (line.amount for item in result.employees for line in item.components if line.code == "pool"),
        Decimal("0"),
    )
    assert paid == Decimal("600.00")


def test_zero_distribution_base_is_rejected() -> None:
    pool = BonusPool(
        "pool", "Премия", Decimal("100"), ("a", "b"), ("a", "b"),
        AllocationMethod.SHIFTS,
    )
    with pytest.raises(ValueError, match="равна нулю"):
        calculate_payroll([employee("a", "0"), employee("b", "0")], bonus_pools=[pool])


def test_automatic_tax_requires_tax_profile() -> None:
    with pytest.raises(ValueError, match="налогового профиля"):
        calculate_payroll([employee("a")], tax_mode=TaxMode.AUTOMATIC)


def test_unconfirmed_salary_basis_is_flagged() -> None:
    item = EmployeeInput("x", "X", Decimal("1000"), SalaryBasis.UNCONFIRMED)
    result = calculate_payroll([item])
    assert result.employees[0].gross_total == Decimal("0.00")
    assert result.employees[0].net_total == Decimal("0.00")
    assert result.employees[0].components[0].category == "unclassified"
    assert result.warnings


def test_net_salary_is_not_misreported_as_gross() -> None:
    net_employee = EmployeeInput(
        "net", "Net Employee", Decimal("40000"), SalaryBasis.NET
    )
    result = calculate_payroll([net_employee])
    item = result.employees[0]
    assert item.gross_total == Decimal("0.00")
    assert item.net_total == Decimal("40000.00")
    assert item.components[0].category == "net_salary"
