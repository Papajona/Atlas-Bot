#!/usr/bin/env python3
"""Best-effort migration for the shipped v2 SQLite schema into v3.

A consistent SQLite online backup is created before any schema/data changes. The
backup API includes committed records that may still reside in the WAL file.
"""
from __future__ import annotations

import sqlite3
import sys
from pathlib import Path


def create_backup(source_path: Path, backup_path: Path) -> None:
    """Create a transactionally consistent SQLite backup, including WAL contents."""
    temporary_path = backup_path.with_name(backup_path.name + ".tmp")
    temporary_path.unlink(missing_ok=True)
    source = sqlite3.connect(source_path)
    destination = None
    try:
        destination = sqlite3.connect(temporary_path)
        source.backup(destination)
        destination.commit()
    except Exception:
        temporary_path.unlink(missing_ok=True)
        raise
    finally:
        if destination is not None:
            destination.close()
        source.close()
    temporary_path.replace(backup_path)


def main() -> None:
    path = Path(sys.argv[1] if len(sys.argv) > 1 else "data/trading.db")
    if not path.exists():
        raise SystemExit(f"Database not found: {path}")
    backup = path.with_suffix(path.suffix + ".bak")
    try:
        create_backup(path, backup)
    except Exception as exc:
        raise SystemExit(f"Could not create a consistent SQLite backup: {exc}") from exc

    conn = sqlite3.connect(path)
    cur = conn.cursor()

    def cols(table: str) -> set[str]:
        return {row[1] for row in cur.execute(f"PRAGMA table_info({table})")}

    try:
        if "trades" in [row[0] for row in cur.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        )]:
            existing_columns = cols("trades")
            adds = {
                "signal_id": "TEXT NOT NULL DEFAULT ''",
                "exchange": "TEXT NOT NULL DEFAULT ''",
                "timeframe": "TEXT NOT NULL DEFAULT ''",
                "filled_quantity": "REAL NOT NULL DEFAULT 0",
                "remaining_quantity": "REAL NOT NULL DEFAULT 0",
                "requested_price": "REAL NOT NULL DEFAULT 0",
                "fee": "REAL NOT NULL DEFAULT 0",
                "notional": "REAL NOT NULL DEFAULT 0",
                "stop_loss_price": "REAL NOT NULL DEFAULT 0",
                "take_profit_price": "REAL NOT NULL DEFAULT 0",
            }
            for name, ddl in adds.items():
                if name not in existing_columns:
                    cur.execute(f"ALTER TABLE trades ADD COLUMN {name} {ddl}")
            cur.execute(
                "UPDATE trades SET signal_id=CASE WHEN signal_id='' THEN client_order_id ELSE signal_id END"
            )
            cur.execute(
                "UPDATE trades SET exchange=CASE WHEN exchange='' THEN 'unknown' ELSE exchange END"
            )
            cur.execute(
                "UPDATE trades SET filled_quantity=quantity WHERE filled_quantity=0 AND status IN ('FILLED','SIMULATED')"
            )
            cur.execute(
                "UPDATE trades SET remaining_quantity=MAX(requested_quantity-filled_quantity,0)"
            )

        cur.execute(
            "CREATE TABLE IF NOT EXISTS positions "
            "(id INTEGER PRIMARY KEY AUTOINCREMENT, exchange TEXT NOT NULL DEFAULT '', "
            "symbol TEXT NOT NULL, quantity REAL NOT NULL DEFAULT 0, "
            "average_entry_price REAL NOT NULL DEFAULT 0, mark_price REAL NOT NULL DEFAULT 0, "
            "realized_pnl REAL NOT NULL DEFAULT 0, unrealized_pnl REAL NOT NULL DEFAULT 0, "
            "updated_at DATETIME)"
        )
        cur.execute("CREATE UNIQUE INDEX IF NOT EXISTS uq_position_symbol ON positions(symbol)")
        cur.execute("CREATE UNIQUE INDEX IF NOT EXISTS uq_trade_signal_id ON trades(signal_id)")
        cur.execute("CREATE INDEX IF NOT EXISTS ix_trade_status_symbol ON trades(status,symbol)")
        cur.execute(
            "CREATE TABLE IF NOT EXISTS audit_log "
            "(id INTEGER PRIMARY KEY AUTOINCREMENT, event TEXT NOT NULL, "
            "detail TEXT NOT NULL DEFAULT '', created_at DATETIME)"
        )
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()
    print(f"Migrated {path}; backup at {backup}")


if __name__ == "__main__":
    main()
