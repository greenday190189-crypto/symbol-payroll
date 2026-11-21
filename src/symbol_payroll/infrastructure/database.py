"""Инициализация локальной SQLite-базы и базовой схемы."""

import sqlite3
from pathlib import Path

SCHEMA_VERSION = 1

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
CREATE TABLE IF NOT EXISTS payroll_snapshots (
    id TEXT PRIMARY KEY,
    period_id TEXT NOT NULL REFERENCES payroll_periods(id),
    employee_id TEXT NOT NULL REFERENCES employees(id),
    snapshot_json TEXT NOT NULL,
    rules_fingerprint TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE(period_id, employee_id)
);
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
        elif row["version"] != SCHEMA_VERSION:
            raise RuntimeError(
                f"Версия БД {row['version']} не поддерживается; ожидается {SCHEMA_VERSION}"
            )
