"""Репозиторий версионируемых правил и жизненного цикла расчётного периода."""

from __future__ import annotations

import hashlib
import json
import sqlite3
from dataclasses import dataclass
from datetime import date, datetime, timezone
from uuid import uuid4

from symbol_payroll.domain.rules import PayrollRule, RuleScope


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
) -> PayrollRule:
    """Создаёт новую версию правила, не перезаписывая старую."""
    if not code.strip() or not scope_id.strip() or not expression.strip():
        raise ValueError("Код, область действия и выражение правила обязательны")
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
               (id, code, scope, scope_id, expression, version, effective_from, effective_to)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (str(uuid4()), code, scope.value, scope_id, expression, version,
             effective_from.isoformat(), effective_to.isoformat() if effective_to else None),
        )
    return PayrollRule(code, scope, scope_id, expression, version, effective_from, effective_to)


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
            bool(row["enabled"]),
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
            snapshot = {
                "employee_id": employee.employee_id,
                "full_name": employee.full_name,
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
