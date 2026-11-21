from datetime import datetime

import pytest

from symbol_payroll.infrastructure.backup import (
    create_backup, default_backup_name, restore_backup, validate_backup,
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
