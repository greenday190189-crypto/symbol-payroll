"""Версионируемые правила и разрешение приоритетов."""

from dataclasses import dataclass
from datetime import date
from enum import IntEnum, StrEnum


class RuleScope(StrEnum):
    ORGANIZATION = "organization"
    STORE = "store"
    POSITION = "position"
    EMPLOYEE = "employee"
    PERIOD = "period"


class RulePriority(IntEnum):
    ORGANIZATION = 10
    STORE = 20
    POSITION = 30
    EMPLOYEE = 40
    PERIOD_OVERRIDE = 50


@dataclass(frozen=True, slots=True)
class PayrollRule:
    code: str
    scope: RuleScope
    scope_id: str
    expression: str
    version: int
    effective_from: date
    effective_to: date | None = None
    enabled: bool = True

    def applies_on(self, day: date) -> bool:
        return self.enabled and self.effective_from <= day and (
            self.effective_to is None or day <= self.effective_to
        )


def select_rule(rules: list[PayrollRule], day: date) -> PayrollRule | None:
    """Выбирает самое специфичное действующее правило; неоднозначность запрещена."""
    candidates = [rule for rule in rules if rule.applies_on(day)]
    if not candidates:
        return None
    priorities = {scope: priority for scope, priority in zip(
        RuleScope, RulePriority, strict=True
    )}
    highest = max(priorities[rule.scope] for rule in candidates)
    winners = [rule for rule in candidates if priorities[rule.scope] == highest]
    if len(winners) != 1:
        raise ValueError(f"Неоднозначные правила для показателя {winners[0].code}")
    return winners[0]
