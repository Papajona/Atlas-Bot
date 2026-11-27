"""Migration coverage for case-insensitive TRON sweep transaction identity."""
from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest
import sqlalchemy as sa
from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy.exc import IntegrityError


ROOT = Path(__file__).resolve().parents[1]
MIGRATION_PATH = ROOT / "alembic" / "versions" / "0068_tron_sweep_txid_casefold_unique.py"


def _load_migration():
    spec = importlib.util.spec_from_file_location("tron_sweep_txid_casefold_migration", MIGRATION_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _run_migration(connection, action):
    migration = _load_migration()
    context = MigrationContext.configure(connection)
    with Operations.context(context):
        getattr(migration, action)()


def _index_sql(connection, index_name: str) -> str | None:
    return connection.execute(
        sa.text(
            "SELECT sql FROM sqlite_master WHERE type = 'index' AND name = :name"
        ),
        {"name": index_name},
    ).scalar_one_or_none()


def _legacy_schema(connection):
    connection.execute(
        sa.text(
            """
            CREATE TABLE tron_sweeps (
                id INTEGER PRIMARY KEY,
                transaction_id VARCHAR(128) NOT NULL DEFAULT ''
            )
            """
        )
    )
    connection.execute(
        sa.text(
            """
            CREATE UNIQUE INDEX uq_tron_sweep_transaction_id_nonempty
            ON tron_sweeps (transaction_id)
            WHERE transaction_id <> ''
            """
        )
    )


def test_migration_replaces_case_sensitive_unique_index_with_casefolded_unique_index():
    engine = sa.create_engine("sqlite://")
    try:
        with engine.begin() as connection:
            _legacy_schema(connection)
            connection.execute(
                sa.text("INSERT INTO tron_sweeps (id, transaction_id) VALUES (1, :txid)"),
                {"txid": "A" * 64},
            )
            _run_migration(connection, "upgrade")

            # Empty transaction IDs are intentionally outside the uniqueness
            # predicate, so pending sweeps can coexist without a chain hash.
            connection.execute(
                sa.text(
                    "INSERT INTO tron_sweeps (id, transaction_id) VALUES "
                    "(3, ''), (4, '')"
                )
            )

            with pytest.raises(IntegrityError):
                with connection.begin_nested():
                    connection.execute(
                        sa.text("INSERT INTO tron_sweeps (id, transaction_id) VALUES (2, :txid)"),
                        {"txid": "a" * 64},
                    )

            casefold_index_sql = _index_sql(
                connection, "uq_tron_sweep_transaction_id_ci_nonempty"
            )
            assert casefold_index_sql is not None
            assert "CREATE UNIQUE INDEX" in casefold_index_sql.upper()
            assert "LOWER(TRANSACTION_ID)" in casefold_index_sql.upper()
            assert "TRANSACTION_ID <> ''" in casefold_index_sql.upper()
            assert _index_sql(
                connection, "uq_tron_sweep_transaction_id_nonempty"
            ) is None
    finally:
        engine.dispose()


def test_migration_rebuilds_a_same_named_index_with_the_wrong_definition():
    engine = sa.create_engine("sqlite://")
    try:
        with engine.begin() as connection:
            _legacy_schema(connection)
            connection.execute(
                sa.text(
                    """
                    CREATE UNIQUE INDEX uq_tron_sweep_transaction_id_ci_nonempty
                    ON tron_sweeps (transaction_id)
                    WHERE transaction_id <> ''
                    """
                )
            )
            connection.execute(
                sa.text("INSERT INTO tron_sweeps (id, transaction_id) VALUES (1, :txid)"),
                {"txid": "A" * 64},
            )

            _run_migration(connection, "upgrade")

            casefold_index_sql = _index_sql(
                connection, "uq_tron_sweep_transaction_id_ci_nonempty"
            )
            assert casefold_index_sql is not None
            assert "LOWER(TRANSACTION_ID)" in casefold_index_sql.upper()
            assert _index_sql(
                connection, "uq_tron_sweep_transaction_id_nonempty"
            ) is None

            with pytest.raises(IntegrityError):
                with connection.begin_nested():
                    connection.execute(
                        sa.text("INSERT INTO tron_sweeps (id, transaction_id) VALUES (2, :txid)"),
                        {"txid": "a" * 64},
                    )
    finally:
        engine.dispose()


def test_postgres_index_validation_checks_the_complete_unique_index_shape():
    migration = _load_migration()

    class Result:
        def __init__(self, row):
            self.row = row

        def mappings(self):
            return self

        def one_or_none(self):
            return self.row

    class Bind:
        def __init__(self, row):
            self.row = row
            self.statement = None

        def execute(self, statement, _params):
            self.statement = str(statement)
            return Result(self.row)

    valid = {
        "indisvalid": True,
        "indisready": True,
        "indislive": True,
        "indisunique": True,
        "indnatts": 1,
        "indnkeyatts": 1,
        "index_expression": "lower(transaction_id)",
        "index_predicate": "(transaction_id <> ''::character varying)",
    }
    bind = Bind(valid)
    assert migration._postgres_index_valid(bind) is True
    assert "indisunique" in bind.statement
    assert "indnkeyatts" in bind.statement
    assert "pg_get_expr(index_data.indexprs" in bind.statement
    assert "pg_get_expr(index_data.indpred" in bind.statement

    wrong_expression = Bind({**valid, "index_expression": "transaction_id"})
    assert migration._postgres_index_valid(wrong_expression) is False

    wrong_predicate = Bind({**valid, "index_predicate": "(transaction_id <> '' AND id > 0)"})
    assert migration._postgres_index_valid(wrong_predicate) is False


def test_migration_refuses_legacy_casefold_collisions_without_dropping_old_guard():
    engine = sa.create_engine("sqlite://")
    try:
        with engine.begin() as connection:
            _legacy_schema(connection)
            connection.execute(
                sa.text("INSERT INTO tron_sweeps (id, transaction_id) VALUES (1, :txid)"),
                {"txid": "A" * 64},
            )
            connection.execute(
                sa.text("INSERT INTO tron_sweeps (id, transaction_id) VALUES (2, :txid)"),
                {"txid": "a" * 64},
            )

            with pytest.raises(RuntimeError, match="case-insensitive TRON sweep transaction uniqueness"):
                _run_migration(connection, "upgrade")

            assert _index_sql(
                connection, "uq_tron_sweep_transaction_id_nonempty"
            ) is not None
            assert _index_sql(
                connection, "uq_tron_sweep_transaction_id_ci_nonempty"
            ) is None
    finally:
        engine.dispose()



def test_migration_downgrade_restores_the_legacy_case_sensitive_index():
    engine = sa.create_engine("sqlite://")
    try:
        with engine.begin() as connection:
            _legacy_schema(connection)
            connection.execute(
                sa.text("INSERT INTO tron_sweeps (id, transaction_id) VALUES (1, :txid)"),
                {"txid": "A" * 64},
            )
            _run_migration(connection, "upgrade")
            _run_migration(connection, "downgrade")

            assert _index_sql(
                connection, "uq_tron_sweep_transaction_id_nonempty"
            ) is not None
            assert _index_sql(
                connection, "uq_tron_sweep_transaction_id_ci_nonempty"
            ) is None

            # Downgrade restores the prior case-sensitive behavior.
            connection.execute(
                sa.text("INSERT INTO tron_sweeps (id, transaction_id) VALUES (2, :txid)"),
                {"txid": "a" * 64},
            )
    finally:
        engine.dispose()


def test_migration_is_the_next_linear_revision_after_service_role_lockdown():
    migration = _load_migration()

    assert migration.revision == "0068_tron_sweep_txid_casefold_unique"
    assert migration.down_revision == "0067_service_role_customer_profiles_acl"
