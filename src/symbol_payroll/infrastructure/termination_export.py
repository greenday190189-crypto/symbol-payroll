"""Экспорт окончательного расчёта увольнения из сохранённого результата."""

from __future__ import annotations

import json
import sqlite3
from decimal import Decimal
from pathlib import Path

from openpyxl import Workbook
from openpyxl.styles import Alignment, Font, PatternFill


def export_termination_workbook(
    connection: sqlite3.Connection, case_id: str, destination: str | Path
) -> Path:
    """Создаёт отдельную книгу по сохранённому расчёту; пересчёт не выполняется."""
    row = connection.execute(
        """SELECT tc.*, e.full_name, e.position, s.name AS store_name
           FROM termination_cases tc
           JOIN employees e ON e.id = tc.employee_id
           JOIN stores s ON s.id = e.store_id
           WHERE tc.id = ?""",
        (case_id,),
    ).fetchone()
    if row is None:
        raise LookupError(f"Дело об увольнении не найдено: {case_id}")
    if not row["result_json"]:
        raise ValueError("Сначала рассчитайте окончательный расчёт увольнения")

    try:
        result = json.loads(row["result_json"])
    except json.JSONDecodeError as exc:
        raise ValueError("Сохранённый результат увольнения повреждён") from exc
    if not isinstance(result, dict):
        raise ValueError("Некорректный формат сохранённого результата увольнения")

    def amount(value, label: str) -> float:
        try:
            number = Decimal(str(value))
        except Exception as exc:
            raise ValueError(f"Некорректная сумма: {label}") from exc
        if not number.is_finite():
            raise ValueError(f"Некорректная сумма: {label}")
        return float(number)

    destination = Path(destination)
    destination.parent.mkdir(parents=True, exist_ok=True)
    workbook = Workbook()
    sheet = workbook.active
    sheet.title = "Окончательный расчёт"
    status = "УТВЕРЖДЁН" if row["status"] == "approved" else "РАССЧИТАН — НЕ УТВЕРЖДЁН"
    sheet.append(["Окончательный расчёт при увольнении", "", "Статус", status])
    sheet.append(["Сотрудник", row["full_name"], "Магазин", row["store_name"]])
    sheet.append(["Должность", row["position"] or "—", "Дата увольнения", row["termination_date"]])
    sheet.append(["ID дела", row["id"], "ID сотрудника", row["employee_id"]])
    sheet.append(["Фактически отработано смен", amount(row["worked_shifts"], "смены"),
                  "Фактически отработано часов", amount(row["worked_hours"], "часы")])
    sheet.append(["Примечание", row["notes"] or "—"])
    sheet.append([])
    sheet.append(["Компонент", "Категория", "Сумма, руб."])
    components = result.get("components", [])
    if not isinstance(components, list):
        raise ValueError("Некорректный список компонентов в сохранённом результате")
    for item in components:
        if not isinstance(item, dict):
            raise ValueError("Некорректная строка компонента в сохранённом результате")
        sheet.append([
            str(item.get("title") or item.get("code") or "Компонент"),
            str(item.get("category") or "unclassified"),
            amount(item.get("amount", "0"), str(item.get("title") or "компонент")),
        ])
    sheet.append([])
    sheet.append(["Итог", "Сумма, руб."])
    for label, key in (
        ("Начислено gross", "gross_total"),
        ("НДФЛ", "tax_amount"),
        ("Удержания", "deduction_total"),
        ("К выплате", "net_total"),
    ):
        sheet.append([label, amount(result.get(key, "0"), label)])
    note = result.get("calculation_note")
    if note:
        sheet.append([])
        sheet.append(["Ограничения расчёта", str(note)])
    warnings = result.get("warnings", [])
    if warnings:
        sheet.append([])
        sheet.append(["Предупреждения"])
        for warning in warnings:
            sheet.append([str(warning)])

    sheet.sheet_view.showGridLines = False
    sheet.freeze_panes = "A8"
    sheet.column_dimensions["A"].width = 42
    sheet.column_dimensions["B"].width = 28
    sheet.column_dimensions["C"].width = 20
    sheet.column_dimensions["D"].width = 28
    for cell in sheet[1]:
        cell.font = Font(bold=True, color="FFFFFF")
        cell.fill = PatternFill("solid", fgColor="263B53")
    for row_cells in (sheet[8],):
        for cell in row_cells:
            cell.font = Font(bold=True, color="FFFFFF")
            cell.fill = PatternFill("solid", fgColor="46627F")
    for row_cells in sheet.iter_rows(min_row=9, min_col=3, max_col=3):
        for cell in row_cells:
            cell.number_format = '#,##0.00;[Red]-#,##0.00'
    for row_cells in sheet.iter_rows():
        for cell in row_cells:
            cell.alignment = Alignment(vertical="top", wrap_text=True)
            if cell.value in {"Итог", "Предупреждения", "Ограничения расчёта"}:
                cell.font = Font(bold=True)
    workbook.properties.title = f"Окончательный расчёт — {row['full_name']}"
    workbook.properties.subject = "Сохранённый расчёт при увольнении"
    workbook.properties.description = (
        "Экспорт сохранённого результата. Повторный расчёт не выполнялся."
    )
    workbook.save(destination)
    return destination
