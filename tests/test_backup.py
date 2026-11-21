from datetime import datetime
import sqlite3

import pytest

from symbol_payroll.infrastructure.backup import (
    create_backup, create_daily_backup, create_pre_migration_backup,
    default_backup_name, restore_backup, validate_backup,
)
from symbol_payroll.infrastructure.database import connect, initialize


@pytest.fixture
def db(tmp_path):
    connection = connect(tmp_path / "live.sqlite3")
    initialize(connection)
    connection.execute("INSERT INTO stores(id, name) VALUES ('s1', 'Сигаево')")
    connection.commit()
    yield connection
    connection.close()


def test_create_and_validate_backup(db, tmp_path):
    destination = tmp_path / "backups" / "copy.sqlite3"
    assert create_backup(db, destination) == destination
    validate_backup(destination)


def test_restore_backup_preserves_current_database_first(db, tmp_path):
    backup = tmp_path / "backup.sqlite3"
    create_backup(db, backup)
    db.execute("UPDATE stores SET name = 'Изменено' WHERE id = 's1'")
    db.commit()

    safety = tmp_path / "before_restore.sqlite3"
    assert restore_backup(db, backup, safety) == safety
    assert db.execute("SELECT name FROM stores WHERE id='s1'").fetchone()["name"] == "Сигаево"
    validate_backup(safety)
    safety_db = connect(safety)
    assert safety_db.execute(
        "SELECT name FROM stores WHERE id='s1'"
    ).fetchone()["name"] == "Изменено"
    safety_db.close()


def test_validate_backup_rejects_non_database_file(tmp_path):
    path = tmp_path / "not-a-db.sqlite3"
    path.write_text("not sqlite", encoding="utf-8")
    with pytest.raises(Exception):
        validate_backup(path)


def test_restore_rejects_invalid_backup_without_touching_live_db(db, tmp_path):
    invalid = tmp_path / "invalid.sqlite3"
    invalid.write_text("invalid", encoding="utf-8")
    with pytest.raises(Exception):
        restore_backup(db, invalid, tmp_path / "safety.sqlite3")
    assert db.execute("SELECT name FROM stores WHERE id='s1'").fetchone()["name"] == "Сигаево"


def test_default_backup_name_is_stable():
    assert default_backup_name(datetime(2026, 11, 21, 12, 30, 45)) == (
        "symbol_payroll_backup_20261121_123045.sqlite3"
    )


def test_daily_backup_creates_one_copy_and_keeps_existing_copy(db, tmp_path, monkeypatch):
    from datetime import datetime
    import symbol_payroll.infrastructure.backup as backup_module

    fixed = datetime(2026, 11, 21, 10, 0, 0)
    monkeypatch.setattr(backup_module, "datetime", type(
        "FixedDateTime", (), {"now": staticmethod(lambda: fixed)}
    ))
    folder = tmp_path / "daily"
    first = create_daily_backup(db, folder)
    second = create_daily_backup(db, folder)
    assert first == second
    assert first.exists()
    assert len(list(folder.glob("symbol_payroll_auto_*.sqlite3"))) == 1


def test_daily_backup_rejects_invalid_retention(db, tmp_path):
    with pytest.raises(ValueError, match="не меньше"):
        create_daily_backup(db, tmp_path, retention=0)



def test_pre_migration_backup_preserves_old_schema_and_data(db, tmp_path) -> None:
    from decimal import Decimal

    from symbol_payroll.domain.pay import SalaryBasis
    from symbol_payroll.infrastructure.repositories import create_employee

    create_employee(
        db, "e1", "s1", "Иванов Иван",
        salary_amount=Decimal("42000"), salary_basis=SalaryBasis.GROSS,
    )
    db.execute("DROP TABLE employee_salary_history")
    db.execute("ALTER TABLE employees DROP COLUMN hired_on")
    db.execute("UPDATE schema_meta SET version = 4")
    db.commit()

    destination = create_pre_migration_backup(
        db, tmp_path / "pre-migration", source_version=4
    )

    preserved = connect(destination)
    assert preserved.execute(
        "SELECT version FROM schema_meta"
    ).fetchone()["version"] == 4
    assert preserved.execute(
        "SELECT salary_amount FROM employees WHERE id='e1'"
    ).fetchone()["salary_amount"] == "42000"
    assert preserved.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    preserved.close()

    # The live database has not been migrated or modified by making the copy.
    assert db.execute(
        "SELECT version FROM schema_meta"
    ).fetchone()["version"] == 4
    assert db.execute(
        "SELECT salary_amount FROM employees WHERE id='e1'"
    ).fetchone()["salary_amount"] == "42000"


def test_pre_migration_backup_rejects_current_schema(db, tmp_path) -> None:
    from symbol_payroll.infrastructure.database import SCHEMA_VERSION

    with pytest.raises(ValueError, match="только для старой версии"):
        create_pre_migration_backup(
            db, tmp_path / "pre-migration", source_version=SCHEMA_VERSION
        )


def test_pre_migration_backup_rejects_mismatched_source_version(db, tmp_path) -> None:
    with pytest.raises(ValueError, match="Версия базы изменилась"):
        create_pre_migration_backup(
            db, tmp_path / "pre-migration", source_version=4
        )
    assert list((tmp_path / "pre-migration").glob("*.sqlite3")) == []



def test_daily_backup_replaces_same_day_copy_from_older_schema(db, tmp_path, monkeypatch):
    import symbol_payroll.infrastructure.backup as backup_module

    fixed = datetime(2026, 11, 22, 12, 0, 0)
    monkeypatch.setattr(backup_module, "datetime", type(
        "FixedDateTime", (), {"now": staticmethod(lambda: fixed)}
    ))
    folder = tmp_path / "daily"
    folder.mkdir()
    legacy_path = folder / "symbol_payroll_auto_20261122_090000.sqlite3"
    legacy = sqlite3.connect(legacy_path)
    legacy.execute("CREATE TABLE schema_meta (version INTEGER NOT NULL)")
    legacy.execute("INSERT INTO schema_meta(version) VALUES (4)")
    legacy.commit()
    legacy.close()

    current = create_daily_backup(db, folder)

    assert current != legacy_path
    assert current.exists()
    validate_backup(current)
    assert legacy_path.exists()
    assert len(list(folder.glob("symbol_payroll_auto_20261122_*.sqlite3"))) == 2
