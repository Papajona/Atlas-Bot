"""Migration 0061 must admit historical outcomes whose fee basis is unknown."""
from __future__ import annotations

import importlib.util
from pathlib import Path

from alembic.operations import Operations
from alembic.runtime.migration import MigrationContext
from sqlalchemy import create_engine, inspect, text


ROOT = Path(__file__).resolve().parents[1]


def test_revision_0061_makes_unknown_net_columns_nullable_before_backfill(monkeypatch):
    migration_path = (
        ROOT / "alembic" / "versions" / "0061_strategy_outcome_fill_aggregation.py"
    )
    spec = importlib.util.spec_from_file_location(
        "migration_0061_unknown_net_fee_test", migration_path
    )
    assert spec is not None and spec.loader is not None
    migration = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(migration)

    engine = create_engine("sqlite://")
    try:
        with engine.begin() as connection:
            # This is the relevant 0035-era schema at revision 0060: all three
            # observed return columns still have NOT NULL constraints.
            connection.execute(text("""
                CREATE TABLE strategy_outcomes (
                    id INTEGER PRIMARY KEY,
                    trade_id INTEGER NOT NULL,
                    closing_trade_id INTEGER,
                    sequence INTEGER NOT NULL DEFAULT 1,
                    customer_id INTEGER,
                    realized_pnl NUMERIC(38, 18) NOT NULL DEFAULT 0,
                    fee NUMERIC(38, 18) NOT NULL DEFAULT 0,
                    net_pnl NUMERIC(38, 18) NOT NULL DEFAULT 0,
                    return_bps FLOAT NOT NULL DEFAULT 0,
                    net_return_bps FLOAT NOT NULL DEFAULT 0,
                    lifecycle_status VARCHAR(20) NOT NULL DEFAULT 'CLOSED',
                    metadata_json TEXT NOT NULL DEFAULT '{}',
                    created_at DATETIME,
                    updated_at DATETIME
                )
            """))
            connection.execute(text("""
                CREATE TABLE positions (
                    id INTEGER PRIMARY KEY,
                    entry_trade_id INTEGER,
                    quantity NUMERIC(38, 18) NOT NULL DEFAULT 0
                )
            """))
            connection.execute(text("""
                CREATE TABLE trades (
                    id INTEGER PRIMARY KEY,
                    filled_quantity NUMERIC(38, 18),
                    average_fill_price NUMERIC(38, 18),
                    notional NUMERIC(38, 18),
                    fee NUMERIC(38, 18),
                    mode VARCHAR(20),
                    customer_id INTEGER,
                    side VARCHAR(10),
                    exchange VARCHAR(50),
                    symbol VARCHAR(80),
                    created_at DATETIME,
                    updated_at DATETIME
                )
            """))
            connection.execute(text("""
                CREATE TABLE trade_learning_episodes (
                    id INTEGER PRIMARY KEY,
                    entry_trade_id INTEGER,
                    entry_quantity NUMERIC(38, 18),
                    status VARCHAR(20),
                    closing_trade_id INTEGER,
                    entry_at DATETIME,
                    exit_at DATETIME
                )
            """))
            connection.execute(text("""
                INSERT INTO strategy_outcomes (
                    id, trade_id, closing_trade_id, sequence, customer_id,
                    realized_pnl, fee, net_pnl, return_bps, net_return_bps,
                    lifecycle_status, metadata_json, created_at
                ) VALUES (
                    1, 100, 200, 1, 42,
                    5, 0, 5, 500, 500,
                    'CLOSED', '{"fill_price":"100"}', '2026-01-01 10:00:00'
                ), (
                    2, 90001, NULL, 1, 42,
                    12.5, 4.75, 12.5, 125.0, 125.0,
                    'CLOSED', '{}', CURRENT_TIMESTAMP
                )
            """))
            connection.execute(text("""
                INSERT INTO trades (
                    id, filled_quantity, average_fill_price, notional, fee,
                    mode, customer_id, side, exchange, symbol, created_at, updated_at
                ) VALUES
                    (100, 1, 100, 100, 0, 'PAPER', 42, 'buy', 'binance',
                     'BTC/USDT', '2026-01-01 08:55:00', '2026-01-01 08:55:00'),
                    (200, 1, 100, 100, 0, 'PAPER', 42, 'sell', 'binance',
                     'BTC/USDT', '2026-01-01 09:54:00', '2026-01-01 10:00:00'),
                    -- Submitted before the old position exited, but filled/updated
                    -- after exit into a new position: must not be attributed.
                    (201, 1, 101, 101, 0, 'PAPER', 42, 'sell', 'binance',
                     'BTC/USDT', '2026-01-01 09:59:00', '2026-01-01 10:01:00')
            """))
            connection.execute(text("""
                INSERT INTO trade_learning_episodes (
                    id, entry_trade_id, entry_quantity, status, closing_trade_id,
                    entry_at, exit_at
                ) VALUES (
                    1, 100, 1, 'COMPLETED', 200,
                    '2026-01-01 09:00:00', '2026-01-01 10:00:00'
                )
            """))

            before = {
                column["name"]: column
                for column in inspect(connection).get_columns("strategy_outcomes")
            }
            assert before["net_pnl"]["nullable"] is False
            assert before["net_return_bps"]["nullable"] is False
            assert before["return_bps"]["nullable"] is False

            monkeypatch.setattr(
                migration.context, "is_offline_mode", lambda: False
            )
            migration_context = MigrationContext.configure(connection)
            with Operations.context(migration_context):
                migration.upgrade()

            after = {
                column["name"]: column
                for column in inspect(connection).get_columns("strategy_outcomes")
            }
            assert after["net_pnl"]["nullable"] is True
            assert after["net_return_bps"]["nullable"] is True
            assert after["return_bps"]["nullable"] is True

            recovered = connection.execute(text("""
                SELECT fee_basis_complete, fee, return_bps, net_pnl, net_return_bps
                FROM strategy_outcomes
                WHERE id = 2
            """)).mappings().one()
            assert recovered["fee_basis_complete"] in (False, 0)
            assert float(recovered["fee"]) == 4.75
            assert recovered["net_pnl"] is None
            assert recovered["net_return_bps"] is None
            assert recovered["return_bps"] is None

            # A second sell order was submitted before exit but only updated
            # after the old position closed. The migration must use the close
            # fill/update timestamp, not submission time, for attribution.
            completed = connection.execute(text("""
                SELECT closing_quantity, closing_notional, lifecycle_status,
                       fee_basis_complete, net_pnl, net_return_bps
                FROM strategy_outcomes
                WHERE id = 1
            """)).mappings().one()
            assert float(completed["closing_quantity"]) == 1.0
            assert float(completed["closing_notional"]) == 100.0
            assert completed["lifecycle_status"] == "CLOSED"
            assert completed["fee_basis_complete"] in (True, 1)
            assert float(completed["net_pnl"]) == 5.0
            assert float(completed["net_return_bps"]) == 500.0
            assert float(completed["return_bps"]) == 500.0
    finally:
        engine.dispose()
