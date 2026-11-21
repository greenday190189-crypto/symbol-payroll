"""Модели оплаты и правила определения суммы оклада."""

from dataclasses import dataclass
from decimal import Decimal
from enum import StrEnum


class SalaryBasis(StrEnum):
    """Как трактуется указанная в карточке сотрудника сумма."""

    GROSS = "gross"
    NET = "net"
    UNCONFIRMED = "unconfirmed"


@dataclass(frozen=True, slots=True)
class SalaryTerms:
    amount: Decimal
    basis: SalaryBasis = SalaryBasis.UNCONFIRMED
    currency: str = "RUB"

    def validate_for_approval(self) -> None:
        if self.amount < 0:
            raise ValueError("Сумма оклада не может быть отрицательной")
        if self.basis is SalaryBasis.UNCONFIRMED:
            raise ValueError(
                "Не определено, указана сумма до НДФЛ или сумма на руки. "
                "Уточните способ задания оклада перед утверждением расчёта."
            )
