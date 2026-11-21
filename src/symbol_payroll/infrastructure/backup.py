"""Безопасное резервное копирование и восстановление SQLite."""

from __future__ import annotations

import sqlite3
from datetime import datetime
from pathlib import Path

from symbol_payroll.infrastructure.database import SCHEMA_VERSION


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


def create_pre_migration_backup(
    connection: sqlite3.Connection,
    directory: str | Path,
    source_version: int,
    retention: int = 30,
) -> Path:
    """Preserve an older schema before initialize() applies a database migration.

    These files intentionally keep their original schema version and therefore
    are emergency recovery copies, not restore candidates for the current UI.
    """
    if source_version >= SCHEMA_VERSION:
        raise ValueError("Предмиграционная копия нужна только для старой версии базы")
    if retention < 1:
        raise ValueError("Срок хранения должен быть не меньше одной копии")

    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    now = datetime.now()
    destination = directory / (
        f"symbol_payroll_pre_migration_v{source_version}_"
        f"{now:%Y%m%d_%H%M%S}.sqlite3"
    )
    if destination.resolve() == Path(
        connection.execute("PRAGMA database_list").fetchone()[2]
    ).resolve():
        raise ValueError("Нельзя перезаписывать рабочую базу резервной копией")

    temporary = destination.with_name(destination.name + ".tmp")
    if temporary.exists():
        temporary.unlink()
    target = sqlite3.connect(temporary)
    try:
        connection.backup(target)
        integrity = target.execute("PRAGMA integrity_check").fetchone()
        if integrity is None or integrity[0] != "ok":
            raise ValueError("Предмиграционная копия не прошла проверку целостности")
        foreign_keys = target.execute("PRAGMA foreign_key_check").fetchall()
        if foreign_keys:
            raise ValueError(
                "Предмиграционная копия содержит нарушения внешних ключей"
            )
        try:
            version = target.execute(
                "SELECT version FROM schema_meta LIMIT 1"
            ).fetchone()
        except sqlite3.DatabaseError as exc:
            raise ValueError("Не удалось прочитать версию старой базы") from exc
        if version is None or int(version[0]) != source_version:
            raise ValueError(
                "Версия базы изменилась во время создания предмиграционной копии"
            )
        target.execute("PRAGMA journal_mode = DELETE")
        target.commit()
    except Exception:
        target.close()
        temporary.unlink(missing_ok=True)
        raise
    else:
        target.close()

    temporary.replace(destination)
    backups = sorted(directory.glob("symbol_payroll_pre_migration_v*_*.sqlite3"))
    for old in backups[:-retention]:
        old.unlink(missing_ok=True)
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


def create_daily_backup(
    connection: sqlite3.Connection, directory: str | Path, retention: int = 30
) -> Path:
    """Создаёт одну автоматическую копию в день и хранит последние retention копий."""
    if retention < 1:
        raise ValueError("Срок хранения должен быть не меньше одной копии")
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    now = datetime.now()
    today = now.strftime("%Y%m%d")
    existing = sorted(directory.glob(f"symbol_payroll_auto_{today}_*.sqlite3"))
    if existing:
        return existing[-1]
    destination = directory / f"symbol_payroll_auto_{today}_{now:%H%M%S}.sqlite3"
    create_backup(connection, destination)
    backups = sorted(directory.glob("symbol_payroll_auto_*.sqlite3"))
    for old in backups[:-retention]:
        old.unlink(missing_ok=True)
    return destination


def restore_backup(
    connection: sqlite3.Connection,
    source: str | Path,
    safety_backup: str | Path,
) -> Path:
    """Сначала сохраняет текущую БД, затем проверяет и восстанавливает копию."""
    source = Path(source)
    database_path = Path(connection.execute("PRAGMA database_list").fetchone()[2])
    if database_path and source.resolve() == database_path.resolve():
        raise ValueError("Нельзя восстанавливать базу из самой себя")
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
