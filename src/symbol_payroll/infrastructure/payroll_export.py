"""Формирование Excel-отчётов только из сохранённых расчётных снимков."""

from __future__ import annotations

import json
import re
import sqlite3
from collections import defaultdict
from decimal import Decimal
from pathlib import Path

from openpyxl import Workbook
from openpyxl.styles import Alignment, Font, PatternFill
from openpyxl.utils import get_column_letter
from openpyxl.worksheet.worksheet import Worksheet


MONEY_FIELDS = ("gross_total", "tax_amount", "deduction_total", "net_total")
HEADERS = ("Сотрудник", "Начислено gross", "НДФЛ", "Удержания", "К выплате")


def _amount(snapshot: dict, field: str) -> Decimal:
    try:
        value = Decimal(str(snapshot.get(field, "0")))
    except Exception as exc:
        raise ValueError(f"Повреждена сумма {field} в расчётном снимке") from exc
    if not value.is_finite():
        raise ValueError(f"Некорректная сумма {field} в расчётном снимке")
    return value


def _sheet_title(value: str, used: set[str]) -> str:
    base = re.sub(r"[\\/*?:\[\]]", " ", value).strip().strip("'") or "Сотрудник"
    base = base[:31]
    candidate = base
    index = 2
    while candidate.casefold() in used:
        suffix = f" ({index})"
        candidate = base[:31 - len(suffix)] + suffix
        index += 1
    used.add(candidate.casefold())
    return candidate


def _style_sheet(sheet: Worksheet) -> None:
    sheet.freeze_panes = "A4"
    sheet.sheet_view.showGridLines = False
    for cell in sheet[1]:
        cell.font = Font(bold=True, color="FFFFFF")
        cell.fill = PatternFill("solid", fgColor="263B53")
        cell.alignment = Alignment(vertical="center")
    sheet.row_dimensions[1].height = 24
    for column in sheet.columns:
        letter = get_column_letter(column[0].column)
        width = min(max(max(len(str(cell.value or "")) for cell in column) + 2, 12), 42)
        sheet.column_dimensions[letter].width = width


def _write_summary_table(sheet: Worksheet, rows: list[dict]) -> None:
    sheet.append(list(HEADERS))
    for row in rows:
        snap = row["snapshot"]
        sheet.append([
            snap.get("full_name", row["employee_id"]),
            float(_amount(snap, "gross_total")),
            float(_amount(snap, "tax_amount")),
            float(_amount(snap, "deduction_total")),
            float(_amount(snap, "net_total")),
        ])
    if rows:
        total_row = sheet.max_row + 1
        sheet.cell(total_row, 1, "ИТОГО")
        for col in range(2, 6):
            letter = get_column_letter(col)
            sheet.cell(total_row, col, f"=SUM({letter}2:{letter}{total_row - 1})")
        for cell in sheet[total_row]:
            cell.font = Font(bold=True)
            cell.fill = PatternFill("solid", fgColor="DCE6F1")
    for row in sheet.iter_rows(min_row=2, min_col=2, max_col=5):
        for cell in row:
            cell.number_format = '#,##0.00;[Red]-#,##0.00'
    _style_sheet(sheet)


def _write_employee_sheet(sheet: Worksheet, snapshot: dict, status: str, period_id: str) -> None:
    sheet.append(["Расчётный листок", period_id, "Статус", status.upper()])
    sheet.append(["Сотрудник", snapshot.get("full_name", "—")])
    sheet.append(["ID сотрудника", snapshot.get("employee_id", "—")])
    sheet.append([])
    sheet.append(["Компонент", "Категория", "Сумма, руб."])
    for component in snapshot.get("components", []):
        amount = Decimal(str(component.get("amount", "0")))
        sheet.append([
            component.get("title") or component.get("code") or "Компонент",
            component.get("category", "unclassified"),
            float(amount),
        ])
    sheet.append([])
    sheet.append(["Итог", "Сумма, руб."])
    for label, field in (
        ("Начислено gross", "gross_total"),
        ("НДФЛ", "tax_amount"),
        ("Удержания", "deduction_total"),
        ("К выплате", "net_total"),
    ):
        sheet.append([label, float(_amount(snapshot, field))])
    warnings = snapshot.get("warnings", [])
    if warnings:
        sheet.append([])
        sheet.append(["Предупреждения расчёта"])
        for warning in warnings:
            sheet.append([str(warning)])
    sheet.sheet_view.showGridLines = False
    sheet.freeze_panes = "A5"
    sheet.column_dimensions["A"].width = 38
    sheet.column_dimensions["B"].width = 24
    sheet.column_dimensions["C"].width = 20
    for cell in sheet[1]:
        cell.font = Font(bold=True, color="FFFFFF")
        cell.fill = PatternFill("solid", fgColor="263B53")
    for row in sheet.iter_rows(min_row=6, min_col=3, max_col=3):
        for cell in row:
            cell.number_format = '#,##0.00;[Red]-#,##0.00'
    for row in sheet.iter_rows():
        if row and row[0].value in {"Итог", "Предупреждения расчёта"}:
            row[0].font = Font(bold=True)


def export_period_workbook(
    connection: sqlite3.Connection, period_id: str, destination: str | Path
) -> Path:
    """Экспортирует сеть, магазины и персональные листки из снимков без пересчёта."""
    period = connection.execute(
        "SELECT id, period_start, period_end, status FROM payroll_periods WHERE id = ?",
        (period_id,),
    ).fetchone()
    if period is None:
        raise LookupError(f"Расчётный период не найден: {period_id}")
    snapshots = connection.execute(
        """SELECT ps.employee_id, ps.snapshot_json, ps.rules_fingerprint,
                  e.store_id, s.name AS store_name
           FROM payroll_snapshots ps
           JOIN employees e ON e.id = ps.employee_id
           JOIN stores s ON s.id = e.store_id
           WHERE ps.period_id = ?
           ORDER BY s.name COLLATE NOCASE, json_extract(ps.snapshot_json, '$.full_name') COLLATE NOCASE""",
        (period_id,),
    ).fetchall()
    if not snapshots:
        raise ValueError("В выбранном периоде нет сохранённых расчётных снимков для экспорта")

    records: list[dict] = []
    for row in snapshots:
        try:
            snapshot = json.loads(row["snapshot_json"])
        except json.JSONDecodeError as exc:
            raise ValueError(f"Повреждён снимок сотрудника {row['employee_id']}") from exc
        if not isinstance(snapshot, dict):
            raise ValueError(f"Некорректный снимок сотрудника {row['employee_id']}")
        records.append({
            "employee_id": row["employee_id"],
            "store_id": row["store_id"],
            "store_name": row["store_name"],
            "snapshot": snapshot,
            "rules_fingerprint": row["rules_fingerprint"],
        })

    destination = Path(destination)
    destination.parent.mkdir(parents=True, exist_ok=True)
    workbook = Workbook()
    summary = workbook.active
    summary.title = "Сводная"
    status_label = {
        "draft": "ЧЕРНОВИК",
        "calculated": "РАССЧИТАНО — НЕ УТВЕРЖДЕНО",
        "approved": "УТВЕРЖДЕНО",
        "closed": "ЗАКРЫТО",
    }.get(period["status"], period["status"].upper())
    summary.append(["Сводная ведомость по сети", period_id, "Статус", status_label])
    summary.append(["Период с", period["period_start"], "по", period["period_end"]])
    summary.append([])
    _write_summary_table(summary, records)
    summary.insert_rows(4)
    summary.cell(4, 1, "Сотрудники сети")
    summary.cell(4, 1).font = Font(bold=True, size=12)

    store_groups: dict[str, list[dict]] = defaultdict(list)
    for record in records:
        store_groups[record["store_name"]].append(record)

    used_titles = {"сводная"}
    store_summary = workbook.create_sheet("По магазинам")
    store_summary.append(["Магазин", "Сотрудников", "Начислено gross", "НДФЛ", "Удержания", "К выплате"])
    for store_name in sorted(store_groups, key=str.casefold):
        group = store_groups[store_name]
        store_summary.append([
            store_name, len(group),
            float(sum((_amount(x["snapshot"], "gross_total") for x in group), Decimal("0"))),
            float(sum((_amount(x["snapshot"], "tax_amount") for x in group), Decimal("0"))),
            float(sum((_amount(x["snapshot"], "deduction_total") for x in group), Decimal("0"))),
            float(sum((_amount(x["snapshot"], "net_total") for x in group), Decimal("0"))),
        ])
        sheet = workbook.create_sheet(_sheet_title(store_name, used_titles))
        sheet.append([f"Ведомость магазина: {store_name}", period_id, "Статус", status_label])
        sheet.append([])
        _write_summary_table(sheet, group)
        sheet.insert_rows(2)
        sheet.cell(2, 1, f"Магазин: {store_name}")
    for row in store_summary.iter_rows(min_row=2, min_col=3, max_col=6):
        for cell in row:
            cell.number_format = '#,##0.00;[Red]-#,##0.00'
    _style_sheet(store_summary)

    for record in records:
        name = record["snapshot"].get("full_name", record["employee_id"])
        sheet = workbook.create_sheet(_sheet_title(f"{name} {record['employee_id']}", used_titles))
        _write_employee_sheet(sheet, record["snapshot"], status_label, period_id)

    workbook.properties.title = f"Расчётная ведомость {period_id}"
    workbook.properties.subject = "Экспорт сохранённых расчётных снимков"
    workbook.properties.description = (
        "Сформировано из неизменённых снимков расчётного периода; повторный расчёт не выполнялся."
    )
    workbook.save(destination)
    return destination
