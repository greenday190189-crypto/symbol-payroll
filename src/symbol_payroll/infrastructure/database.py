"""Инициализация локальной SQLite-базы и базовой схемы."""

import sqlite3
from pathlib import Path

SCHEMA_VERSION = 3

_SCHEMA = """
CREATE TABLE IF NOT EXISTS schema_meta (
    version INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS stores (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL UNIQUE,
    active INTEGER NOT NULL DEFAULT 1 CHECK (active IN (0, 1))
);
CREATE TABLE IF NOT EXISTS employees (
    id TEXT PRIMARY KEY,
    store_id TEXT NOT NULL REFERENCES stores(id),
    full_name TEXT NOT NULL,
    position TEXT,
    active INTEGER NOT NULL DEFAULT 1 CHECK (active IN (0, 1)),
    salary_amount TEXT,
    salary_basis TEXT NOT NULL DEFAULT 'unconfirmed'
        CHECK (salary_basis IN ('gross', 'net', 'unconfirmed'))
);
CREATE TABLE IF NOT EXISTS payroll_rules (
    id TEXT PRIMARY KEY,
    code TEXT NOT NULL,
    scope TEXT NOT NULL CHECK (scope IN ('organization','store','position','employee','period')),
    scope_id TEXT NOT NULL,
    expression TEXT NOT NULL,
    category TEXT NOT NULL DEFAULT 'earning'
        CHECK (category IN ('earning','bonus','net_salary','deduction','unclassified')),
    title TEXT NOT NULL DEFAULT '',
    version INTEGER NOT NULL CHECK (version > 0),
    effective_from TEXT NOT NULL,
    effective_to TEXT,
    enabled INTEGER NOT NULL DEFAULT 1 CHECK (enabled IN (0, 1)),
    UNIQUE(code, scope, scope_id, version)
);
CREATE TABLE IF NOT EXISTS payroll_periods (
    id TEXT PRIMARY KEY,
    period_start TEXT NOT NULL,
    period_end TEXT NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('draft','calculated','approved','closed')),
    created_at TEXT NOT NULL,
    approved_at TEXT
);
CREATE TABLE IF NOT EXISTS payroll_inputs (
    period_id TEXT NOT NULL REFERENCES payroll_periods(id),
    employee_id TEXT NOT NULL REFERENCES employees(id),
    shifts TEXT NOT NULL DEFAULT '0',
    hours TEXT NOT NULL DEFAULT '0',
    sales TEXT NOT NULL DEFAULT '0',
    plan TEXT NOT NULL DEFAULT '0',
    advances TEXT NOT NULL DEFAULT '0',
    deductions TEXT NOT NULL DEFAULT '0',
    manual_tax TEXT NOT NULL DEFAULT '0',
    fixed_adjustments TEXT NOT NULL DEFAULT '0',
    extra_values_json TEXT NOT NULL DEFAULT '{}',
    updated_at TEXT NOT NULL,
    PRIMARY KEY(period_id, employee_id)
);
CREATE TABLE IF NOT EXISTS payroll_snapshots (
    id TEXT PRIMARY KEY,
    period_id TEXT NOT NULL REFERENCES payroll_periods(id),
    employee_id TEXT NOT NULL REFERENCES employees(id),
    snapshot_json TEXT NOT NULL,
    rules_fingerprint TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE(period_id, employee_id)
);
CREATE TRIGGER IF NOT EXISTS payroll_rules_no_update
BEFORE UPDATE ON payroll_rules
BEGIN
    SELECT RAISE(ABORT, 'Payroll rules are immutable; create a new version');
END;
CREATE TRIGGER IF NOT EXISTS payroll_rules_no_delete
BEFORE DELETE ON payroll_rules
BEGIN
    SELECT RAISE(ABORT, 'Payroll rules are immutable and cannot be deleted');
END;
CREATE TRIGGER IF NOT EXISTS approved_snapshots_no_update
BEFORE UPDATE ON payroll_snapshots
WHEN (SELECT status FROM payroll_periods WHERE id = OLD.period_id) IN ('approved', 'closed')
BEGIN
    SELECT RAISE(ABORT, 'Approved payroll snapshots are immutable');
END;
CREATE TRIGGER IF NOT EXISTS approved_snapshots_no_delete
BEFORE DELETE ON payroll_snapshots
WHEN (SELECT status FROM payroll_periods WHERE id = OLD.period_id) IN ('approved', 'closed')
BEGIN
    SELECT RAISE(ABORT, 'Approved payroll snapshots are immutable');
END;
CREATE TRIGGER IF NOT EXISTS approved_snapshots_no_insert
BEFORE INSERT ON payroll_snapshots
WHEN (SELECT status FROM payroll_periods WHERE id = NEW.period_id) IN ('approved', 'closed')
BEGIN
    SELECT RAISE(ABORT, 'Cannot add snapshots to an approved payroll period');
END;
CREATE TRIGGER IF NOT EXISTS approved_inputs_no_update
BEFORE UPDATE ON payroll_inputs
WHEN (SELECT status FROM payroll_periods WHERE id = OLD.period_id) IN ('approved', 'closed')
BEGIN
    SELECT RAISE(ABORT, 'Approved payroll inputs are immutable');
END;
CREATE TRIGGER IF NOT EXISTS approved_inputs_no_delete
BEFORE DELETE ON payroll_inputs
WHEN (SELECT status FROM payroll_periods WHERE id = OLD.period_id) IN ('approved', 'closed')
BEGIN
    SELECT RAISE(ABORT, 'Approved payroll inputs are immutable');
END;
CREATE TRIGGER IF NOT EXISTS approved_inputs_no_insert
BEFORE INSERT ON payroll_inputs
WHEN (SELECT status FROM payroll_periods WHERE id = NEW.period_id) IN ('approved', 'closed')
BEGIN
    SELECT RAISE(ABORT, 'Cannot add inputs to an approved payroll period');
END;
"""


def connect(path: str | Path) -> sqlite3.Connection:
    db_path = Path(path)
    db_path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(db_path)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = ON")
    return connection


def initialize(connection: sqlite3.Connection) -> None:
    with connection:
        connection.executescript(_SCHEMA)
        row = connection.execute("SELECT version FROM schema_meta LIMIT 1").fetchone()
        if row is None:
            connection.execute("INSERT INTO schema_meta(version) VALUES (?)", (SCHEMA_VERSION,))
        elif row["version"] == 1:
            columns = {item["name"] for item in connection.execute("PRAGMA table_info(payroll_rules)")}
            if "category" not in columns:
                connection.execute(
                    "ALTER TABLE payroll_rules ADD COLUMN category TEXT NOT NULL DEFAULT 'earning'"
                )
            if "title" not in columns:
                connection.execute(
                    "ALTER TABLE payroll_rules ADD COLUMN title TEXT NOT NULL DEFAULT ''"
                )
            connection.execute(
                """CREATE TABLE IF NOT EXISTS payroll_inputs (
                    period_id TEXT NOT NULL REFERENCES payroll_periods(id),
                    employee_id TEXT NOT NULL REFERENCES employees(id),
                    shifts TEXT NOT NULL DEFAULT '0',
                    hours TEXT NOT NULL DEFAULT '0',
                    sales TEXT NOT NULL DEFAULT '0',
                    plan TEXT NOT NULL DEFAULT '0',
                    advances TEXT NOT NULL DEFAULT '0',
                    deductions TEXT NOT NULL DEFAULT '0',
                    manual_tax TEXT NOT NULL DEFAULT '0',
                    fixed_adjustments TEXT NOT NULL DEFAULT '0',
                    extra_values_json TEXT NOT NULL DEFAULT '{}',
                    updated_at TEXT NOT NULL,
                    PRIMARY KEY(period_id, employee_id)
                )"""
            )
            connection.execute("UPDATE schema_meta SET version = ?", (SCHEMA_VERSION,))
        elif row["version"] == 2:
            columns = {item["name"] for item in connection.execute("PRAGMA table_info(payroll_rules)")}
            if "category" not in columns:
                connection.execute(
                    "ALTER TABLE payroll_rules ADD COLUMN category TEXT NOT NULL DEFAULT 'earning'"
                )
            if "title" not in columns:
                connection.execute(
                    "ALTER TABLE payroll_rules ADD COLUMN title TEXT NOT NULL DEFAULT ''"
                )
            connection.execute(
                """CREATE TABLE IF NOT EXISTS payroll_inputs (
                    period_id TEXT NOT NULL REFERENCES payroll_periods(id),
                    employee_id TEXT NOT NULL REFERENCES employees(id),
                    shifts TEXT NOT NULL DEFAULT '0',
                    hours TEXT NOT NULL DEFAULT '0',
                    sales TEXT NOT NULL DEFAULT '0',
                    plan TEXT NOT NULL DEFAULT '0',
                    advances TEXT NOT NULL DEFAULT '0',
                    deductions TEXT NOT NULL DEFAULT '0',
                    manual_tax TEXT NOT NULL DEFAULT '0',
                    fixed_adjustments TEXT NOT NULL DEFAULT '0',
                    extra_values_json TEXT NOT NULL DEFAULT '{}',
                    updated_at TEXT NOT NULL,
                    PRIMARY KEY(period_id, employee_id)
                )"""
            )
            connection.execute("UPDATE schema_meta SET version = ?", (SCHEMA_VERSION,))
        elif row["version"] != SCHEMA_VERSION:
            raise RuntimeError(
                f"Версия БД {row['version']} не поддерживается; ожидается {SCHEMA_VERSION}"
            )
