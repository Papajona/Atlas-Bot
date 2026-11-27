"""Regression tests for revision 0064's platform outcome indexes."""
from importlib.util import module_from_spec, spec_from_file_location
from pathlib import Path

from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import create_engine, inspect


ROOT = Path(__file__).resolve().parents[1]
MIGRATION_PATH = ROOT / "alembic" / "versions" / "0064_strategy_outcome_recent_index.py"
SPEC = spec_from_file_location("strategy_outcome_recent_index_0064", MIGRATION_PATH)
MIGRATION = module_from_spec(SPEC)
assert SPEC is not None and SPEC.loader is not None
SPEC.loader.exec_module(MIGRATION)


def _columns(engine):
    index = next(
        item for item in inspect(engine).get_indexes("strategy_outcomes")
        if item["name"] == "ix_strategy_outcome_platform_closed_trade_time"
    )
    return list(index["column_names"])


def test_0064_upgrade_replaces_revision_0060_two_column_index_and_downgrade_restores_it():
    engine = create_engine("sqlite://")
    try:
        with engine.begin() as connection:
            connection.exec_driver_sql(
                "CREATE TABLE strategy_outcomes ("
                "id INTEGER PRIMARY KEY, trade_id INTEGER NOT NULL, "
                "customer_id INTEGER, lifecycle_status TEXT NOT NULL, created_at DATETIME)"
            )
            connection.exec_driver_sql(
                "CREATE INDEX ix_strategy_outcome_platform_closed_trade_time "
                "ON strategy_outcomes(trade_id, created_at) "
                "WHERE customer_id IS NULL AND lifecycle_status = 'CLOSED'"
            )
            with Operations.context(MigrationContext.configure(connection)):
                MIGRATION.upgrade()
            assert _columns(connection) == ["trade_id", "created_at", "id"]

            with Operations.context(MigrationContext.configure(connection)):
                MIGRATION.downgrade()
            assert _columns(connection) == ["trade_id", "created_at"]
    finally:
        engine.dispose()


def test_0064_upgrade_keeps_existing_three_column_trade_index_idempotent():
    engine = create_engine("sqlite://")
    try:
        with engine.begin() as connection:
            connection.exec_driver_sql(
                "CREATE TABLE strategy_outcomes ("
                "id INTEGER PRIMARY KEY, trade_id INTEGER NOT NULL, "
                "customer_id INTEGER, lifecycle_status TEXT NOT NULL, created_at DATETIME)"
            )
            connection.exec_driver_sql(
                "CREATE INDEX ix_strategy_outcome_platform_closed_trade_time "
                "ON strategy_outcomes(trade_id, created_at, id) "
                "WHERE customer_id IS NULL AND lifecycle_status = 'CLOSED'"
            )
            with Operations.context(MigrationContext.configure(connection)):
                MIGRATION.upgrade()
            assert _columns(connection) == ["trade_id", "created_at", "id"]
    finally:
        engine.dispose()
