"""Migration 0061 must admit historical outcomes whose fee basis is unknown."""
from __future__ import annotations

import importlib.util
from decimal import Decimal

import pytest
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
                    5, 0, 5, 123, 500,
                    'CLOSED', '{"fill_price":"100"}', '2026-01-01 10:00:00'
                ), (
                    2, 90001, NULL, 1, 42,
                    12.5, 0, 12.5, 125.0, 125.0,
                    'CLOSED', '{}', CURRENT_TIMESTAMP
                )
            """))
            connection.execute(text("""
                INSERT INTO trades (
                    id, filled_quantity, average_fill_price, notional, fee,
                    mode, customer_id, side, exchange, symbol, created_at, updated_at
                ) VALUES
                    (100, 1, 100, 100, 0.4, 'PAPER', 42, 'buy', 'binance',
                     'BTC/USDT', '2026-01-01 08:55:00', '2026-01-01 08:55:00'),
                    (200, 1, 100, 100, 0, 'PAPER', 42, 'sell', 'binance',
                     'BTC/USDT', '2026-01-01 09:54:00', '2026-01-01 10:00:00'),
                    -- Submitted before the old position exited, but filled/updated
                    -- after exit into a new position: must not be attributed.
                    (201, 1, 101, 101, 0, 'PAPER', 42, 'sell', 'binance',
                     'BTC/USDT', '2026-01-01 09:59:00', '2026-01-01 10:01:00')
            """))
            # A still-open paper position exercises the later entry-fee update.
            # Its 1-unit remainder fully reconciles to the 1-unit entry order.
            connection.execute(text("""
                INSERT INTO positions (id, entry_trade_id, quantity)
                VALUES (1, 100, 1)
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
            seeded_gross_return_bps = connection.execute(text("""
                SELECT return_bps FROM strategy_outcomes WHERE id = 1
            """)).scalar_one()
            assert float(seeded_gross_return_bps) == pytest.approx(123.0)

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
                SELECT fee_basis_complete, return_bps, net_pnl, net_return_bps
                FROM strategy_outcomes
                WHERE id = 2
            """)).mappings().one()
            assert recovered["fee_basis_complete"] in (False, 0)
            assert recovered["net_pnl"] is None
            assert recovered["net_return_bps"] is None
            assert recovered["return_bps"] is None

            # A second sell order was submitted before exit but only updated
            # after the old position closed. The migration must use the close
            # fill/update timestamp, not submission time, for attribution.
            completed = connection.execute(text("""
                SELECT closing_quantity, closing_notional, lifecycle_status,
                       fee_basis_complete, return_bps, net_pnl, net_return_bps
                FROM strategy_outcomes
                WHERE id = 1
            """)).mappings().one()
            assert float(completed["closing_quantity"]) == 1.0
            assert float(completed["closing_notional"]) == 100.0
            assert completed["lifecycle_status"] == "CLOSED"
            assert completed["fee_basis_complete"] in (True, 1)
            # Gross realized PnL is 5.0; the verified 0.4 entry fee makes
            # the reconstructed net PnL 4.6 (460 bps on 100 notional).
            assert float(completed["net_pnl"]) == pytest.approx(4.6)
            assert float(completed["net_return_bps"]) == pytest.approx(460.0)
            assert float(completed["return_bps"]) == pytest.approx(500.0)
            assert float(completed["return_bps"]) != float(seeded_gross_return_bps)

            remaining_position = connection.execute(text("""
                SELECT entry_fee, entry_fee_basis_known
                FROM positions
                WHERE id = 1
            """)).mappings().one()
            assert float(remaining_position["entry_fee"]) == pytest.approx(0.4)
            assert remaining_position["entry_fee_basis_known"] in (True, 1)
    finally:
        engine.dispose()



def test_reconstruct_notional_skips_non_finite_historical_return_evidence():
    migration_path = (
        ROOT / "alembic" / "versions" / "0061_strategy_outcome_fill_aggregation.py"
    )
    spec = importlib.util.spec_from_file_location(
        "migration_0061_non_finite_evidence_test", migration_path
    )
    assert spec is not None and spec.loader is not None
    migration = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(migration)

    net_nan = {
        "closing_notional": float("nan"),
        "net_return_bps": float("nan"),
        "net_pnl": 10,
        "return_bps": 50,
        "realized_pnl": 100,
    }
    assert migration._reconstruct_notional(net_nan) == Decimal("20000")

    gross_nan = {
        "closing_notional": 0,
        "net_return_bps": 0,
        "net_pnl": 0,
        "return_bps": float("nan"),
        "realized_pnl": 100,
    }
    assert migration._reconstruct_notional(gross_nan) == Decimal("0")



def test_revision_0061_recombines_legacy_blanket_closed_rows_when_episode_evidence_matches():
    migration_path = (
        ROOT / "alembic" / "versions" / "0061_strategy_outcome_fill_aggregation.py"
    )
    spec = importlib.util.spec_from_file_location(
        "migration_0061_legacy_closed_lifecycle_test", migration_path
    )
    assert spec is not None and spec.loader is not None
    migration = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(migration)

    rows = [
        {
            "trade_id": 1,
            "closing_trade_id": 11,
            "sequence": 1,
            "lifecycle_status": "CLOSED",
        },
        {
            "trade_id": 1,
            "closing_trade_id": 12,
            "sequence": 2,
            "lifecycle_status": "CLOSED",
        },
    ]
    source_trades = {
        11: {"id": 11, "filled_quantity": Decimal("0.4")},
        12: {"id": 12, "filled_quantity": Decimal("0.6")},
    }
    episodes = {1: {"entry_quantity": Decimal("1.0"), "status": "COMPLETED"}}

    assert migration._legacy_closed_rows_are_one_lifecycle(
        rows, source_trades, episodes
    )


def test_revision_0061_keeps_legacy_closed_rows_split_when_quantity_evidence_conflicts():
    migration_path = (
        ROOT / "alembic" / "versions" / "0061_strategy_outcome_fill_aggregation.py"
    )
    spec = importlib.util.spec_from_file_location(
        "migration_0061_legacy_closed_lifecycle_negative_test", migration_path
    )
    assert spec is not None and spec.loader is not None
    migration = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(migration)

    rows = [
        {
            "trade_id": 1,
            "closing_trade_id": 11,
            "sequence": 1,
            "lifecycle_status": "CLOSED",
        },
        {
            "trade_id": 1,
            "closing_trade_id": 12,
            "sequence": 2,
            "lifecycle_status": "CLOSED",
        },
    ]
    source_trades = {
        11: {"id": 11, "filled_quantity": Decimal("0.4")},
        12: {"id": 12, "filled_quantity": Decimal("0.3")},
    }
    episodes = {1: {"entry_quantity": Decimal("1.0"), "status": "COMPLETED"}}

    assert not migration._legacy_closed_rows_are_one_lifecycle(
        rows, source_trades, episodes
    )
