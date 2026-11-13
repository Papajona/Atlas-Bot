from __future__ import annotations

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
