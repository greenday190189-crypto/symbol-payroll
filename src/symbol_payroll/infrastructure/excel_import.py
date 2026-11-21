"""Безопасный предпросмотр и импорт месячных показателей из Excel."""

from __future__ import annotations

import re
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Mapping

from openpyxl import load_workbook

from symbol_payroll.infrastructure.period_inputs import PeriodEmployeeInput


ALIASES: dict[str, tuple[str, ...]] = {
    "employee_name": ("фио", "фио сотрудника", "сотрудник", "работник", "имя сотрудника", "employee", "full name"),
    "shifts": ("смены", "смен", "количество смен", "отработано смен"),
    "hours": ("часы", "часов", "отработано часов", "часы факт"),
    "sales": ("продажи", "выручка", "оборот", "сумма продаж"),
    "plan": ("план", "план продаж", "план выручки"),
    "advances": ("аванс", "авансы", "выплачено авансом"),
    "deductions": ("удержания", "удержание", "прочие удержания"),
    "manual_tax": ("ндфл", "налог", "удержан ндфл"),
    "fixed_adjustments": ("корректировка", "корректировки", "доплата", "adjustment"),
}

FIELD_LABELS = {
    "employee_name": "Сотрудник / ФИО",
    "shifts": "Смены",
    "hours": "Часы",
    "sales": "Продажи",
    "plan": "План",
    "advances": "Аванс",
    "deductions": "Удержания",
    "manual_tax": "НДФЛ",
    "fixed_adjustments": "Корректировка",
}
NUMERIC_FIELDS = tuple(key for key in FIELD_LABELS if key != "employee_name")


@dataclass(frozen=True, slots=True)
class ColumnMapping:
    employee_name: int | None
    shifts: int | None = None
    hours: int | None = None
    sales: int | None = None
    plan: int | None = None
    advances: int | None = None
    deductions: int | None = None
    manual_tax: int | None = None
    fixed_adjustments: int | None = None


@dataclass(frozen=True, slots=True)
class ImportPreviewRow:
    source_row: int
    employee_id: str | None
    employee_name: str
    values: Mapping[str, Decimal]
    errors: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class ExcelImportPreview:
    sheet_name: str
    store_id: str
    rows: tuple[ImportPreviewRow, ...]

    @property
    def errors(self) -> tuple[str, ...]:
        return tuple(
            f"Строка {row.source_row}: {error}"
            for row in self.rows for error in row.errors
        )

    @property
    def valid_count(self) -> int:
        return sum(1 for row in self.rows if row.employee_id and not row.errors)


def list_workbook_sheets(path: str | Path) -> list[str]:
    workbook = load_workbook(path, read_only=True, data_only=True)
    try:
        return list(workbook.sheetnames)
    finally:
        workbook.close()


def read_headers(path: str | Path, sheet_name: str, header_row: int = 1) -> list[str]:
    if header_row < 1:
        raise ValueError("Номер строки заголовков должен быть больше нуля")
    workbook = load_workbook(path, read_only=True, data_only=True)
    try:
        if sheet_name not in workbook.sheetnames:
            raise ValueError(f"Лист не найден: {sheet_name}")
        sheet = workbook[sheet_name]
        return [
            "" if value is None else str(value).strip()
            for value in next(sheet.iter_rows(
                min_row=header_row, max_row=header_row, values_only=True
            ), ())
        ]
    finally:
        workbook.close()


def _normalize_header(value: object) -> str:
    return re.sub(r"\s+", " ", str(value or "").strip().casefold().replace("ё", "е"))


def suggest_mapping(headers: list[str]) -> ColumnMapping:
    normalized = [_normalize_header(value) for value in headers]
    selected: dict[str, int | None] = {}
    for field, aliases in ALIASES.items():
        alias_set = {_normalize_header(alias) for alias in aliases}
        selected[field] = next(
            (index for index, header in enumerate(normalized) if header in alias_set),
            None,
        )
    return ColumnMapping(**selected)


def _decimal_value(value: object, field: str, row_number: int) -> Decimal:
    if value is None or (isinstance(value, str) and not value.strip()):
        return Decimal("0")
    if isinstance(value, bool):
        raise ValueError(f"Строка {row_number}: поле «{FIELD_LABELS[field]}» не является числом")
    raw = str(value).replace("\u00a0", "").replace(" ", "").replace(",", ".")
    raw = raw.replace("₽", "").replace("руб.", "").replace("руб", "")
    try:
        result = Decimal(raw)
    except InvalidOperation as exc:
        raise ValueError(
            f"Строка {row_number}: некорректное число в поле «{FIELD_LABELS[field]}»: {value}"
        ) from exc
    if not result.is_finite() or result < 0:
        raise ValueError(
            f"Строка {row_number}: поле «{FIELD_LABELS[field]}» должно быть конечным и неотрицательным"
        )
    return result


def build_import_preview(
    connection: sqlite3.Connection,
    path: str | Path,
    sheet_name: str,
    store_id: str,
    header_row: int,
    mapping: ColumnMapping,
) -> ExcelImportPreview:
    if mapping.employee_name is None:
        raise ValueError("Не сопоставлена обязательная колонка «Сотрудник / ФИО»")
    store = connection.execute(
        "SELECT id FROM stores WHERE id = ? AND active = 1", (store_id,)
    ).fetchone()
    if store is None:
        raise LookupError("Выбранный магазин не найден или неактивен")
    employees = connection.execute(
        "SELECT id, full_name FROM employees WHERE store_id = ? AND active = 1",
        (store_id,),
    ).fetchall()
    by_name: dict[str, list[str]] = {}
    for employee in employees:
        normalized = _normalize_header(employee["full_name"])
        by_name.setdefault(normalized, []).append(employee["id"])

    workbook = load_workbook(path, read_only=True, data_only=True)
    try:
        if sheet_name not in workbook.sheetnames:
            raise ValueError(f"Лист не найден: {sheet_name}")
        sheet = workbook[sheet_name]
        rows: list[ImportPreviewRow] = []
        seen: set[str] = set()
        for row_number, raw_row in enumerate(
            sheet.iter_rows(min_row=header_row + 1, values_only=True),
            start=header_row + 1,
        ):
            if not raw_row or all(value is None or str(value).strip() == "" for value in raw_row):
                continue
            name_value = raw_row[mapping.employee_name] if mapping.employee_name < len(raw_row) else None
            employee_name = str(name_value or "").strip()
            errors: list[str] = []
            if not employee_name:
                errors.append("не заполнено ФИО сотрудника")
                employee_id = None
            else:
                matches = by_name.get(_normalize_header(employee_name), [])
                if not matches:
                    errors.append(f"сотрудник «{employee_name}» не найден в выбранном магазине")
                    employee_id = None
                elif len(matches) > 1:
                    errors.append(f"неоднозначное совпадение сотрудника «{employee_name}»")
                    employee_id = None
                else:
                    employee_id = matches[0]
                    if employee_id in seen:
                        errors.append(f"сотрудник «{employee_name}» повторяется в файле")
                    seen.add(employee_id)

            values: dict[str, Decimal] = {}
            for field in NUMERIC_FIELDS:
                index = getattr(mapping, field)
                raw_value = raw_row[index] if index is not None and index < len(raw_row) else None
                try:
                    values[field] = _decimal_value(raw_value, field, row_number)
                except ValueError as exc:
                    errors.append(str(exc))
                    values[field] = Decimal("0")
            rows.append(ImportPreviewRow(
                row_number, employee_id, employee_name, values, tuple(errors)
            ))
        if not rows:
            raise ValueError("На выбранном листе нет строк с данными для импорта")
        return ExcelImportPreview(sheet_name, store_id, tuple(rows))
    finally:
        workbook.close()


def apply_import_preview(
    connection: sqlite3.Connection,
    period_id: str,
    preview: ExcelImportPreview,
) -> int:
    if preview.errors:
        raise ValueError(
            "Импорт отменён: исправьте ошибки предпросмотра. " + "; ".join(preview.errors[:8])
        )
    if not preview.rows or preview.valid_count != len(preview.rows):
        raise ValueError("Нет полностью проверенных строк для импорта")
    period = connection.execute(
        "SELECT status FROM payroll_periods WHERE id = ?", (period_id,)
    ).fetchone()
    if period is None:
        raise LookupError(f"Расчётный период не найден: {period_id}")
    if period["status"] not in ("draft", "calculated"):
        raise ValueError("Нельзя импортировать данные в утверждённый период")

    prepared: list[PeriodEmployeeInput] = []
    for row in preview.rows:
        if row.employee_id is None:
            raise ValueError(f"Строка {row.source_row}: не определён сотрудник")
        item = PeriodEmployeeInput(
            period_id, row.employee_id,
            shifts=row.values.get("shifts", Decimal("0")),
            hours=row.values.get("hours", Decimal("0")),
            sales=row.values.get("sales", Decimal("0")),
            plan=row.values.get("plan", Decimal("0")),
            advances=row.values.get("advances", Decimal("0")),
            deductions=row.values.get("deductions", Decimal("0")),
            manual_tax=row.values.get("manual_tax", Decimal("0")),
            fixed_adjustments=row.values.get("fixed_adjustments", Decimal("0")),
        )
        item.validate()
        prepared.append(item)

    with connection:
        if period["status"] == "calculated":
            connection.execute(
                "DELETE FROM payroll_snapshots WHERE period_id = ?", (period_id,)
            )
            connection.execute(
                "UPDATE payroll_periods SET status = 'draft' WHERE id = ?", (period_id,)
            )
        now = datetime.now(timezone.utc).isoformat()
        for item in prepared:
            connection.execute(
                """INSERT INTO payroll_inputs (
                    period_id, employee_id, shifts, hours, sales, plan, advances,
                    deductions, manual_tax, fixed_adjustments, extra_values_json, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, '{}', ?)
                ON CONFLICT(period_id, employee_id) DO UPDATE SET
                    shifts=excluded.shifts, hours=excluded.hours, sales=excluded.sales,
                    plan=excluded.plan, advances=excluded.advances,
                    deductions=excluded.deductions, manual_tax=excluded.manual_tax,
                    fixed_adjustments=excluded.fixed_adjustments,
                    extra_values_json='{}', updated_at=excluded.updated_at""",
                (
                    item.period_id, item.employee_id, str(item.shifts), str(item.hours),
                    str(item.sales), str(item.plan), str(item.advances),
                    str(item.deductions), str(item.manual_tax),
                    str(item.fixed_adjustments), now,
                ),
            )
    return len(prepared)
