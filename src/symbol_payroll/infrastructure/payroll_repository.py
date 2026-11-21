"""Репозиторий версионируемых правил и жизненного цикла расчётного периода."""

from __future__ import annotations

import hashlib
import json
import sqlite3
from dataclasses import dataclass
from datetime import date, datetime, timezone
from uuid import uuid4

from symbol_payroll.domain.rules import PayrollRule, RuleScope
from symbol_payroll.infrastructure.repositories import invalidate_calculated_periods


@dataclass(frozen=True, slots=True)
class PayrollPeriodRecord:
    id: str
    period_start: date
    period_end: date
    status: str


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def save_rule_version(
    connection: sqlite3.Connection,
    code: str,
    scope: RuleScope,
    scope_id: str,
    expression: str,
    effective_from: date,
    effective_to: date | None = None,
    category: str = "earning",
    title: str = "",
) -> PayrollRule:
    """Создаёт новую версию правила, не перезаписывая старую."""
    if not code.strip() or not scope_id.strip() or not expression.strip():
        raise ValueError("Код, область действия и выражение правила обязательны")
    if category not in {"earning", "bonus", "net_salary", "deduction", "unclassified"}:
        raise ValueError(f"Недопустимая категория правила: {category}")
    if effective_to is not None and effective_to < effective_from:
        raise ValueError("Дата окончания правила раньше даты начала")
    with connection:
        row = connection.execute(
            """SELECT COALESCE(MAX(version), 0) AS version FROM payroll_rules
               WHERE code = ? AND scope = ? AND scope_id = ?""",
            (code, scope.value, scope_id),
        ).fetchone()
        version = int(row["version"]) + 1
        connection.execute(
            """INSERT INTO payroll_rules
               (id, code, scope, scope_id, expression, category, title, version, effective_from, effective_to)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (str(uuid4()), code, scope.value, scope_id, expression, category, title or code, version,
             effective_from.isoformat(), effective_to.isoformat() if effective_to else None),
        )
        # A new immutable rule version makes every unapproved calculation potentially stale.
        invalidate_calculated_periods(connection)
    return PayrollRule(
        code, scope, scope_id, expression, version, effective_from, effective_to,
        True, category, title or code,
    )


def list_rule_versions(
    connection: sqlite3.Connection, code: str | None = None
) -> list[PayrollRule]:
    query = "SELECT * FROM payroll_rules"
    params: tuple[str, ...] = ()
    if code is not None:
        query += " WHERE code = ?"
        params = (code,)
    query += " ORDER BY code, scope, scope_id, version"
    return [
        PayrollRule(
            row["code"], RuleScope(row["scope"]), row["scope_id"], row["expression"],
            row["version"], date.fromisoformat(row["effective_from"]),
            date.fromisoformat(row["effective_to"]) if row["effective_to"] else None,
            bool(row["enabled"]), row["category"], row["title"] or row["code"],
        )
        for row in connection.execute(query, params)
    ]


def create_period(
    connection: sqlite3.Connection, period_id: str, start: date, end: date
) -> PayrollPeriodRecord:
    if end < start:
        raise ValueError("Дата окончания периода раньше даты начала")
    with connection:
        connection.execute(
            """INSERT INTO payroll_periods
               (id, period_start, period_end, status, created_at)
               VALUES (?, ?, ?, 'draft', ?)""",
            (period_id, start.isoformat(), end.isoformat(), _now()),
        )
    return PayrollPeriodRecord(period_id, start, end, "draft")


def save_employee_snapshot(
    connection: sqlite3.Connection,
    period_id: str,
    employee_id: str,
    snapshot: dict,
    rule_versions: list[str],
) -> str:
    """Сохраняет расчётный снимок с контрольным отпечатком применённых версий."""
    payload = json.dumps(snapshot, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    fingerprint_source = json.dumps(sorted(rule_versions), separators=(",", ":"))
    fingerprint = hashlib.sha256(fingerprint_source.encode("utf-8")).hexdigest()
    snapshot_id = str(uuid4())
    with connection:
        period = connection.execute(
            "SELECT status FROM payroll_periods WHERE id = ?", (period_id,)
        ).fetchone()
        if period is None:
            raise LookupError(f"Расчётный период не найден: {period_id}")
        if period["status"] not in ("draft", "calculated"):
            raise ValueError("Нельзя изменять снимки утверждённого или закрытого периода")
        connection.execute(
            """INSERT INTO payroll_snapshots
               (id, period_id, employee_id, snapshot_json, rules_fingerprint, created_at)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (snapshot_id, period_id, employee_id, payload, fingerprint, _now()),
        )
    return snapshot_id


_APPROVAL_BLOCKING_WARNING_MARKERS = (
    "тип оклада gross/net не определён",
    "вводные показатели за период не заполнены",
    "увольнение внутри периода; полный месячный оклад автоматически не начислен",
)


def list_approval_blockers(
    connection: sqlite3.Connection, period_id: str
) -> list[str]:
    """Return unique critical warnings that make a payroll period unsafe to approve."""
    rows = connection.execute(
        "SELECT snapshot_json FROM payroll_snapshots WHERE period_id = ?",
        (period_id,),
    ).fetchall()
    blockers: set[str] = set()
    for row in rows:
        try:
            snapshot = json.loads(row["snapshot_json"])
        except (TypeError, json.JSONDecodeError):
            blockers.add("Расчётный снимок повреждён: не удалось прочитать JSON")
            continue
        if not isinstance(snapshot, dict):
            blockers.add("Расчётный снимок повреждён: ожидается JSON-объект")
            continue
        warnings = snapshot.get("warnings", [])
        if not isinstance(warnings, list):
            blockers.add("Расчётный снимок повреждён: список предупреждений имеет неверный формат")
            continue
        for warning in warnings:
            if not isinstance(warning, str):
                blockers.add("Расчётный снимок повреждён: предупреждение имеет неверный формат")
                continue
            if any(marker in warning for marker in _APPROVAL_BLOCKING_WARNING_MARKERS):
                blockers.add(warning)
    return sorted(blockers)


def transition_period(
    connection: sqlite3.Connection, period_id: str, target_status: str
) -> None:
    allowed = {"draft": "calculated", "calculated": "approved", "approved": "closed"}
    with connection:
        row = connection.execute(
            "SELECT status FROM payroll_periods WHERE id = ?", (period_id,)
        ).fetchone()
        if row is None:
            raise LookupError(f"Расчётный период не найден: {period_id}")
        current = row["status"]
        if allowed.get(current) != target_status:
            raise ValueError(f"Недопустимый переход периода: {current} → {target_status}")
        if target_status == "approved":
            count = connection.execute(
                "SELECT COUNT(*) AS count FROM payroll_snapshots WHERE period_id = ?",
                (period_id,),
            ).fetchone()["count"]
            if count == 0:
                raise ValueError("Нельзя утвердить период без сохранённых расчётных снимков")
            blockers = list_approval_blockers(connection, period_id)
            if blockers:
                details = "\n".join(f"• {warning}" for warning in blockers[:12])
                remaining = len(blockers) - 12
                suffix = f"\n…и ещё {remaining}" if remaining > 0 else ""
                raise ValueError(
                    "Нельзя утвердить ведомость: устраните критические предупреждения.\n"
                    f"{details}{suffix}"
                )
        approved_at = _now() if target_status == "approved" else None
        connection.execute(
            "UPDATE payroll_periods SET status = ?, approved_at = COALESCE(?, approved_at) WHERE id = ?",
            (target_status, approved_at, period_id),
        )


def persist_payroll_result(
    connection: sqlite3.Connection,
    period_id: str,
    result,
    rule_versions: list[str],
) -> None:
    """Атомарно сохраняет полный результат расчёта как черновые снимки периода.

    Повторный расчёт draft/calculated периода заменяет предыдущий набор снимков.
    После утверждения эта операция запрещена.
    """
    fingerprint_source = json.dumps(sorted(rule_versions), separators=(",", ":"))
    fingerprint = hashlib.sha256(fingerprint_source.encode("utf-8")).hexdigest()
    with connection:
        period = connection.execute(
            "SELECT status FROM payroll_periods WHERE id = ?", (period_id,)
        ).fetchone()
        if period is None:
            raise LookupError(f"Расчётный период не найден: {period_id}")
        if period["status"] not in ("draft", "calculated"):
            raise ValueError("Нельзя пересчитывать утверждённый или закрытый период")
        connection.execute("DELETE FROM payroll_snapshots WHERE period_id = ?", (period_id,))
        for employee in result.employees:
            directory = connection.execute(
                """SELECT e.store_id, e.position, s.name AS store_name
                   FROM employees e JOIN stores s ON s.id = e.store_id
                   WHERE e.id = ?""",
                (employee.employee_id,),
            ).fetchone()
            snapshot = {
                "employee_id": employee.employee_id,
                "full_name": employee.full_name,
                "store_id": directory["store_id"] if directory else None,
                "store_name": directory["store_name"] if directory else "",
                "position": directory["position"] if directory else None,
                "components": [
                    {
                        "code": line.code,
                        "title": line.title,
                        "amount": str(line.amount),
                        "category": line.category,
                    }
                    for line in employee.components
                ],
                "gross_total": str(employee.gross_total),
                "tax_amount": str(employee.tax_amount),
                "deduction_total": str(employee.deduction_total),
                "net_total": str(employee.net_total),
                "warnings": list(result.warnings),
            }
            payload = json.dumps(
                snapshot, ensure_ascii=False, sort_keys=True, separators=(",", ":")
            )
            connection.execute(
                """INSERT INTO payroll_snapshots
                   (id, period_id, employee_id, snapshot_json, rules_fingerprint, created_at)
                   VALUES (?, ?, ?, ?, ?, ?)""",
                (str(uuid4()), period_id, employee.employee_id, payload, fingerprint, _now()),
            )
        if period["status"] == "draft":
            connection.execute(
                "UPDATE payroll_periods SET status = 'calculated' WHERE id = ?",
                (period_id,),
            )


def list_period_snapshots(
    connection: sqlite3.Connection, period_id: str
) -> list[dict]:
    """Возвращает сохранённые снимки с восстановленным JSON-представлением."""
    return [
        {
            "employee_id": row["employee_id"],
            "snapshot": json.loads(row["snapshot_json"]),
            "rules_fingerprint": row["rules_fingerprint"],
            "created_at": row["created_at"],
        }
        for row in connection.execute(
            """SELECT employee_id, snapshot_json, rules_fingerprint, created_at
               FROM payroll_snapshots WHERE period_id = ? ORDER BY employee_id""",
            (period_id,),
        )
    ]



def list_periods(connection: sqlite3.Connection) -> list[PayrollPeriodRecord]:
    """Возвращает периоды от новых к старым."""
    return [
        PayrollPeriodRecord(
            row["id"], date.fromisoformat(row["period_start"]),
            date.fromisoformat(row["period_end"]), row["status"],
        )
        for row in connection.execute(
            """SELECT id, period_start, period_end, status
               FROM payroll_periods ORDER BY period_start DESC, id DESC"""
        )
    ]
