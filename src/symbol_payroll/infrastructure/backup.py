"""Безопасное резервное копирование и восстановление SQLite."""

from __future__ import annotations

import sqlite3
from datetime import datetime
from pathlib import Path

from symbol_payroll.infrastructure.database import SCHEMA_VERSION, connect


def _validate_database(connection: sqlite3.Connection) -> None:
    result = connection.execute("PRAGMA integrity_check").fetchone()
    if result is None or result[0] != "ok":
        raise ValueError("Резервная копия повреждена: проверка целостности не пройдена")
    foreign_keys = connection.execute("PRAGMA foreign_key_check").fetchall()
    if foreign_keys:
        raise ValueError("Резервная копия содержит нарушения внешних ключей")
    try:
        version = connection.execute(
            "SELECT version FROM schema_meta LIMIT 1"
        ).fetchone()
    except sqlite3.DatabaseError as exc:
        raise ValueError("Файл не является базой Symbol Payroll") from exc
    if version is None or version[0] != SCHEMA_VERSION:
        raise ValueError(
            f"Неподдерживаемая версия базы: {None if version is None else version[0]}"
        )


def create_backup(
    connection: sqlite3.Connection, destination: str | Path
) -> Path:
    """Создаёт согласованную копию открытой БД через SQLite backup API."""
    destination = Path(destination)
    destination.parent.mkdir(parents=True, exist_ok=True)
    if destination.exists() and destination.resolve() == Path(
        connection.execute("PRAGMA database_list").fetchone()[2]
    ).resolve():
        raise ValueError("Нельзя перезаписывать рабочую базу резервной копией")
    temporary = destination.with_name(destination.name + ".tmp")
    if temporary.exists():
        temporary.unlink()
    target = sqlite3.connect(temporary)
    try:
        connection.backup(target)
        target.execute("PRAGMA journal_mode = DELETE")
        _validate_database(target)
        target.commit()
    except Exception:
        target.close()
        temporary.unlink(missing_ok=True)
        raise
    else:
        target.close()
    temporary.replace(destination)
    return destination


def validate_backup(source: str | Path) -> None:
    """Проверяет целостность, внешние ключи и совместимость схемы."""
    source = Path(source)
    if not source.is_file():
        raise FileNotFoundError(source)
    backup = sqlite3.connect(f"file:{source.resolve().as_posix()}?mode=ro", uri=True)
    try:
        _validate_database(backup)
    finally:
        backup.close()


def restore_backup(
    connection: sqlite3.Connection,
    source: str | Path,
    safety_backup: str | Path,
) -> Path:
    """Сначала сохраняет текущую БД, затем проверяет и восстанавливает копию."""
    source = Path(source)
    validate_backup(source)
    safety_path = create_backup(connection, safety_backup)
    backup = sqlite3.connect(f"file:{source.resolve().as_posix()}?mode=ro", uri=True)
    try:
        backup.backup(connection)
        _validate_database(connection)
    except Exception:
        backup.close()
        raise
    finally:
        if backup:
            backup.close()
    return safety_path


def default_backup_name(now: datetime | None = None) -> str:
    stamp = (now or datetime.now()).strftime("%Y%m%d_%H%M%S")
    return f"symbol_payroll_backup_{stamp}.sqlite3"
