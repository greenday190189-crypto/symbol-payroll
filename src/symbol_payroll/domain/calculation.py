"""Расчёт начислений и распределяемых премиальных фондов.

Ядро не содержит встроенных ставок магазинов или налоговых ставок. Все суммы и
параметры поступают из подтверждённой конфигурации конкретного расчётного периода.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal, ROUND_HALF_UP
from enum import StrEnum
from typing import Protocol

from symbol_payroll.domain.pay import SalaryBasis
from symbol_payroll.domain.tax import TaxMode


CENT = Decimal("0.01")
ZERO = Decimal("0")


def money(value: Decimal) -> Decimal:
    return value.quantize(CENT, rounding=ROUND_HALF_UP)


class AllocationMethod(StrEnum):
    EQUAL = "equal"
    SHIFTS = "shifts"
    HOURS = "hours"
    WEIGHTS = "weights"


@dataclass(frozen=True, slots=True)
class EmployeeInput:
    employee_id: str
    full_name: str
    salary_amount: Decimal | None
    salary_basis: SalaryBasis
    shifts: Decimal = ZERO
    hours: Decimal = ZERO
    allocation_weight: Decimal = Decimal("1")
    fixed_adjustments: Decimal = ZERO
    manual_tax: Decimal = ZERO

    def validate(self) -> None:
        if not self.employee_id or not self.full_name.strip():
            raise ValueError("У сотрудника должны быть идентификатор и ФИО")
        for field_name in ("shifts", "hours", "allocation_weight", "fixed_adjustments", "manual_tax"):
            if getattr(self, field_name) < ZERO:
                raise ValueError(f"Параметр {field_name} не может быть отрицательным")
        if self.salary_amount is not None and self.salary_amount < ZERO:
            raise ValueError("Оклад не может быть отрицательным")
        if self.manual_tax > ZERO and self.salary_basis is SalaryBasis.UNCONFIRMED:
            raise ValueError("Нельзя проверить НДФЛ: тип оклада gross/net не определён")


@dataclass(frozen=True, slots=True)
class PayComponent:
    code: str
    title: str
    amount: Decimal
    category: str = "earning"


@dataclass(frozen=True, slots=True)
class BonusPool:
    code: str
    title: str
    amount: Decimal
    participants: tuple[str, ...]
    recipients: tuple[str, ...]
    method: AllocationMethod
    weights: dict[str, Decimal] = field(default_factory=dict)

    def validate(self, employee_ids: set[str]) -> None:
        if self.amount < ZERO:
            raise ValueError(f"Премиальный фонд {self.code}: сумма отрицательная")
        if not self.participants or not self.recipients:
            raise ValueError(f"Премиальный фонд {self.code}: не заданы участники или получатели")
        if len(set(self.participants)) != len(self.participants):
            raise ValueError(f"Премиальный фонд {self.code}: повторяются участники")
        if len(set(self.recipients)) != len(self.recipients):
            raise ValueError(f"Премиальный фонд {self.code}: повторяются получатели")
        unknown = (set(self.participants) | set(self.recipients)) - employee_ids
        if unknown:
            raise ValueError(f"Премиальный фонд {self.code}: неизвестные сотрудники {sorted(unknown)}")
        if self.method is AllocationMethod.WEIGHTS:
            if any(self.weights.get(person, ZERO) < ZERO for person in self.recipients):
                raise ValueError("Вес распределения не может быть отрицательным")
            if sum((self.weights.get(person, ZERO) for person in self.recipients), ZERO) <= ZERO:
                raise ValueError("Сумма весов получателей должна быть больше нуля")


@dataclass(frozen=True, slots=True)
class EmployeeResult:
    employee_id: str
    full_name: str
    components: tuple[PayComponent, ...]
    gross_total: Decimal
    tax_amount: Decimal
    net_total: Decimal


@dataclass(frozen=True, slots=True)
class PayrollResult:
    employees: tuple[EmployeeResult, ...]
    total_gross: Decimal
    total_tax: Decimal
    total_net: Decimal
    warnings: tuple[str, ...]


class TaxCalculator(Protocol):
    def calculate(self, employee: EmployeeInput, taxable_base: Decimal) -> Decimal: ...


def _allocate(pool: BonusPool, employees: dict[str, EmployeeInput]) -> dict[str, Decimal]:
    """Распределяет фонд между получателями с точностью до копейки."""
    if pool.method is AllocationMethod.EQUAL:
        bases = {person: Decimal("1") for person in pool.recipients}
    elif pool.method is AllocationMethod.SHIFTS:
        bases = {person: employees[person].shifts for person in pool.recipients}
    elif pool.method is AllocationMethod.HOURS:
        bases = {person: employees[person].hours for person in pool.recipients}
    else:
        bases = {person: pool.weights.get(person, ZERO) for person in pool.recipients}

    denominator = sum(bases.values(), ZERO)
    if denominator <= ZERO:
        raise ValueError(
            f"Премиальный фонд {pool.code}: сумма базы распределения равна нулю"
        )

    result: dict[str, Decimal] = {}
    remaining = money(pool.amount)
    for index, person in enumerate(pool.recipients):
        if index == len(pool.recipients) - 1:
            share = remaining
        else:
            share = money(pool.amount * bases[person] / denominator)
            remaining -= share
        result[person] = share
    return result


def calculate_payroll(
    employees: list[EmployeeInput],
    components: dict[str, list[PayComponent]] | None = None,
    bonus_pools: list[BonusPool] | None = None,
    tax_mode: TaxMode = TaxMode.DISABLED,
    tax_calculator: TaxCalculator | None = None,
) -> PayrollResult:
    """Рассчитывает черновую ведомость. Неизвестные типы оклада не конвертируются."""
    components = components or {}
    bonus_pools = bonus_pools or []
    by_id = {employee.employee_id: employee for employee in employees}
    if len(by_id) != len(employees):
        raise ValueError("В списке сотрудников есть повторяющиеся идентификаторы")
    for employee in employees:
        employee.validate()

    result_components: dict[str, list[PayComponent]] = {key: [] for key in by_id}
    warnings: list[str] = []

    for employee_id, employee_components in components.items():
        if employee_id not in by_id:
            raise ValueError(f"Компоненты начислений ссылаются на неизвестного сотрудника {employee_id}")
        for component in employee_components:
            if component.amount < ZERO:
                raise ValueError(f"Компонент {component.code}: сумма не может быть отрицательной")
            result_components[employee_id].append(component)

    for employee in employees:
        if employee.salary_amount is None:
            warnings.append(f"{employee.full_name}: оклад не задан")
            continue
        if employee.salary_basis is SalaryBasis.UNCONFIRMED:
            warnings.append(
                f"{employee.full_name}: тип оклада gross/net не определён; "
                "значение включено в начисления без пересчёта НДФЛ"
            )
        elif employee.salary_basis is SalaryBasis.GROSS:
            result_components[employee.employee_id].append(
                PayComponent("base_salary", "Оклад", money(employee.salary_amount))
            )
        else:
            warnings.append(
                f"{employee.full_name}: оклад задан на руки; автоматический пересчёт gross "
                "не выполняется без налогового профиля"
            )
            result_components[employee.employee_id].append(
                PayComponent("base_salary_net", "Оклад (сумма на руки)", money(employee.salary_amount))
            )
        if employee.fixed_adjustments:
            result_components[employee.employee_id].append(
                PayComponent("adjustment", "Дополнительная корректировка",
                             money(employee.fixed_adjustments))
            )

    for pool in bonus_pools:
        pool.validate(set(by_id))
        allocations = _allocate(pool, by_id)
        for employee_id, amount in allocations.items():
            result_components[employee_id].append(
                PayComponent(pool.code, pool.title, amount, "bonus")
            )

    if tax_mode is TaxMode.AUTOMATIC and tax_calculator is None:
        raise ValueError("Автоматический НДФЛ недоступен без подключённого налогового профиля")
    if tax_mode in (TaxMode.MANUAL, TaxMode.IMPORTED):
        pass
    elif tax_mode is TaxMode.DISABLED:
        pass

    employee_results: list[EmployeeResult] = []
    for employee in employees:
        lines = result_components[employee.employee_id]
        gross = money(sum((line.amount for line in lines), ZERO))
        if tax_mode is TaxMode.AUTOMATIC:
            assert tax_calculator is not None
            tax = money(tax_calculator.calculate(employee, gross))
        elif tax_mode in (TaxMode.MANUAL, TaxMode.IMPORTED):
            tax = money(employee.manual_tax)
        else:
            tax = ZERO
        if tax > gross:
            raise ValueError(f"НДФЛ превышает начисления сотрудника {employee.full_name}")
        employee_results.append(EmployeeResult(
            employee.employee_id, employee.full_name, tuple(lines), gross, tax, money(gross - tax)
        ))

    return PayrollResult(
        tuple(employee_results),
        money(sum((item.gross_total for item in employee_results), ZERO)),
        money(sum((item.tax_amount for item in employee_results), ZERO)),
        money(sum((item.net_total for item in employee_results), ZERO)),
        tuple(warnings),
    )
