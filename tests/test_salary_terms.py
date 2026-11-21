from decimal import Decimal

import pytest

from symbol_payroll.domain.pay import SalaryBasis, SalaryTerms


def test_unconfirmed_salary_basis_blocks_approval() -> None:
    terms = SalaryTerms(Decimal("50000"))
    with pytest.raises(ValueError, match="Не определено"):
        terms.validate_for_approval()


def test_gross_salary_can_be_approved() -> None:
    SalaryTerms(Decimal("50000"), SalaryBasis.GROSS).validate_for_approval()


def test_net_salary_can_be_approved() -> None:
    SalaryTerms(Decimal("50000"), SalaryBasis.NET).validate_for_approval()


def test_negative_salary_is_rejected() -> None:
    with pytest.raises(ValueError, match="отрицательной"):
        SalaryTerms(Decimal("-1"), SalaryBasis.GROSS).validate_for_approval()
