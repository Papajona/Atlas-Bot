from __future__ import annotations

import os
import sqlite3

from scripts.migrate_v2_sqlite import create_backup


def test_sqlite_online_backup_includes_committed_wal_records(tmp_path):
    source_path = tmp_path / "trading.db"
    backup_path = tmp_path / "trading.db.bak"

    writer = sqlite3.connect(source_path)
    try:
        assert writer.execute("PRAGMA journal_mode=WAL").fetchone()[0].lower() == "wal"
        writer.execute("PRAGMA wal_autocheckpoint=0")
        writer.execute("CREATE TABLE audit_log (id INTEGER PRIMARY KEY, detail TEXT NOT NULL)")
        writer.commit()
        writer.execute("INSERT INTO audit_log (detail) VALUES ('committed-in-wal')")
        writer.commit()

        wal_path = tmp_path / "trading.db-wal"
        assert wal_path.exists()
        assert wal_path.stat().st_size > 0

        create_backup(source_path, backup_path)

        with sqlite3.connect(backup_path) as backup:
            copied = backup.execute("SELECT detail FROM audit_log ORDER BY id").fetchall()
        assert copied == [("committed-in-wal",)]

        with sqlite3.connect(source_path) as source:
            original = source.execute("SELECT detail FROM audit_log ORDER BY id").fetchall()
        assert original == copied
    finally:
        writer.close()


def test_sqlite_online_backup_replaces_previous_backup_atomically(tmp_path):
    source_path = tmp_path / "source.db"
    backup_path = tmp_path / "source.db.bak"

    with sqlite3.connect(source_path) as source:
        source.execute("CREATE TABLE values_table (value TEXT NOT NULL)")
        source.execute("INSERT INTO values_table VALUES ('new-data')")
    backup_path.write_bytes(b"stale-backup")

    create_backup(source_path, backup_path)

    with sqlite3.connect(backup_path) as backup:
        assert backup.execute("SELECT value FROM values_table").fetchall() == [("new-data",)]
    assert not (tmp_path / "source.db.bak.tmp").exists()


def test_sqlite_backup_is_private_for_new_file_under_permissive_umask(tmp_path):
    source_path = tmp_path / "new-source.db"
    backup_path = tmp_path / "new-source.db.bak"
    with sqlite3.connect(source_path) as source:
        source.execute("CREATE TABLE private_data (value TEXT NOT NULL)")
        source.execute("INSERT INTO private_data VALUES ('secret')")

    previous_umask = os.umask(0o022)
    try:
        create_backup(source_path, backup_path)
    finally:
        os.umask(previous_umask)

    assert backup_path.stat().st_mode & 0o777 == 0o600


def test_sqlite_backup_preserves_restrictive_mode_on_replacement_under_umask(tmp_path):
    source_path = tmp_path / "restricted-source.db"
    backup_path = tmp_path / "restricted-source.db.bak"
    with sqlite3.connect(source_path) as source:
        source.execute("CREATE TABLE private_data (value TEXT NOT NULL)")
        source.execute("INSERT INTO private_data VALUES ('secret')")

    backup_path.write_bytes(b"old backup")
    backup_path.chmod(0o600)
    previous_umask = os.umask(0o022)
    try:
        create_backup(source_path, backup_path)
    finally:
        os.umask(previous_umask)

    assert backup_path.stat().st_mode & 0o777 == 0o600
    with sqlite3.connect(backup_path) as backup:
        assert backup.execute("SELECT value FROM private_data").fetchall() == [("secret",)]
    assert not (tmp_path / "restricted-source.db.bak.tmp").exists()
