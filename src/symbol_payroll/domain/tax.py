"""Контракт налогового расчёта. Не содержит неподтверждённых налоговых ставок."""

from dataclasses import dataclass
from decimal import Decimal
from enum import StrEnum


class TaxMode(StrEnum):
    AUTOMATIC = "automatic"
    MANUAL = "manual"
    IMPORTED = "imported"
    DISABLED = "disabled"


@dataclass(frozen=True, slots=True)
class TaxResult:
    taxable_base: Decimal
    tax_amount: Decimal
    rule_version: str | None
    mode: TaxMode

    def __post_init__(self) -> None:
        if self.taxable_base < 0 or self.tax_amount < 0:
            raise ValueError("Налоговая база и НДФЛ не могут быть отрицательными")
        if self.mode is TaxMode.AUTOMATIC and not self.rule_version:
            raise ValueError("Для автоматического НДФЛ требуется версия налоговых правил")
