"""Regression tests for Alembic 0052 index-state downgrade behavior."""
import asyncio
import importlib.util
import os
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
MIGRATION_PATH = ROOT / "alembic" / "versions" / "0052_schema_metadata_reconciliation.py"
_SPEC = importlib.util.spec_from_file_location("migration_0052_schema_metadata_reconciliation", MIGRATION_PATH)
assert _SPEC is not None and _SPEC.loader is not None
_migration = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(_migration)


def test_downgrade_restores_missing_legacy_index_with_its_table(monkeypatch):
    restored = []
    monkeypatch.setattr(_migration, "_index_names", lambda _bind, _table, schema="public": set())

    def restore(_bind, table, name, definition, comment, schema="public"):
        restored.append((table, name, definition, comment, schema))

    monkeypatch.setattr(_migration, "_restore_index_definition", restore)
    _migration._restore_rename_index_state(None, {
        "table_name": "adaptive_trading_bots",
        "old_name": "ix_adaptive_bot_strategy_candidate_id",
        "new_name": "ix_adaptive_trading_bots_strategy_candidate_id",
        "old_existed": True,
        "new_existed": False,
        "old_definition": "CREATE INDEX ix_adaptive_bot_strategy_candidate_id ON public.adaptive_trading_bots (strategy_candidate_id)",
        "old_comment": "legacy-index",
        "new_definition": None,
        "new_comment": None,
    })
    assert restored == [(
        "adaptive_trading_bots",
        "ix_adaptive_bot_strategy_candidate_id",
        "CREATE INDEX ix_adaptive_bot_strategy_candidate_id ON public.adaptive_trading_bots (strategy_candidate_id)",
        "legacy-index", "public",
    )]


def test_downgrade_restores_missing_canonical_index_with_its_table(monkeypatch):
    restored = []
    monkeypatch.setattr(_migration, "_index_names", lambda _bind, _table, schema="public": set())

    def restore(_bind, table, name, definition, comment, schema="public"):
        restored.append((table, name, definition, comment, schema))

    monkeypatch.setattr(_migration, "_restore_index_definition", restore)
    _migration._restore_rename_index_state(None, {
        "table_name": "order_commands",
        "old_name": "ix_order_command_customer_id",
        "new_name": "ix_order_commands_customer_id",
        "old_existed": False,
        "new_existed": True,
        "old_definition": None,
        "old_comment": None,
        "new_definition": "CREATE INDEX ix_order_commands_customer_id ON public.order_commands (customer_id)",
        "new_comment": "canonical-index",
    })
    assert restored == [(
        "order_commands",
        "ix_order_commands_customer_id",
        "CREATE INDEX ix_order_commands_customer_id ON public.order_commands (customer_id)",
        "canonical-index", "public",
    )]


def test_downgrade_drops_customer_indexes_created_when_both_names_were_absent(monkeypatch):
    """Both the historical and canonical names were absent before upgrade."""
    newly_created = {
        ("adaptive_trading_bots", "ix_adaptive_trading_bots_strategy_candidate_id"),
        ("order_commands", "ix_order_commands_customer_id"),
    }
    initial = set(newly_created)

    def index_names(_bind, table, schema="public"):
        return {name for index_table, name in newly_created if index_table == table}

    def drop_index(_bind, table, name, schema="public"):
        newly_created.discard((table, name))

    monkeypatch.setattr(_migration, "_index_names", index_names)
    monkeypatch.setattr(_migration, "_drop_index", drop_index)

    for table, old_name, new_name in (
        (
            "adaptive_trading_bots",
            "ix_adaptive_bot_strategy_candidate_id",
            "ix_adaptive_trading_bots_strategy_candidate_id",
        ),
        (
            "order_commands",
            "ix_order_command_customer_id",
            "ix_order_commands_customer_id",
        ),
    ):
        _migration._restore_rename_index_state(None, {
            "table_name": table,
            "old_name": old_name,
            "new_name": new_name,
            "old_existed": False,
            "new_existed": False,
            "old_definition": None,
            "old_comment": None,
            "new_definition": None,
            "new_comment": None,
        })

    assert initial == {
        ("adaptive_trading_bots", "ix_adaptive_trading_bots_strategy_candidate_id"),
        ("order_commands", "ix_order_commands_customer_id"),
    }
    assert newly_created == set()


def test_downgrade_preserves_preexisting_canonical_index_when_legacy_name_was_absent(monkeypatch):
    canonical = ("order_commands", "ix_order_commands_customer_id")
    current = {canonical}
    monkeypatch.setattr(
        _migration,
        "_index_names",
        lambda _bind, table, schema="public": {name for index_table, name in current if index_table == table},
    )

    def must_not_drop(_bind, table, name):
        current.discard((table, name))

    monkeypatch.setattr(_migration, "_drop_index", must_not_drop)
    _migration._restore_rename_index_state(None, {
        "table_name": "order_commands",
        "old_name": "ix_order_command_customer_id",
        "new_name": "ix_order_commands_customer_id",
        "old_existed": False,
        "new_existed": True,
        "old_definition": None,
        "old_comment": None,
        "new_definition": "CREATE INDEX ix_order_commands_customer_id ON public.order_commands USING btree (customer_id)",
        "new_comment": None,
    })

    assert current == {canonical}



def test_upgrade_refuses_to_reuse_preexisting_migration_provenance_schema():
    """Do not change ACLs or trust rollback metadata in a pre-existing reserved schema."""
    import pytest

    class ScalarResult:
        def scalar_one(self):
            return True  # exact pg_namespace lookup confirms the reserved schema exists

    class ExistingSchemaBind:
        def execute(self, *_args, **_kwargs):
            return ScalarResult()

    with pytest.raises(RuntimeError, match="reserved migration schema atlas_migration_meta already exists"):
        _migration._capture_rename_index_state(ExistingSchemaBind())



@pytest.mark.postgres
@pytest.mark.skipif(
    not os.getenv("ATLAS_POSTGRES_URL"),
    reason="requires the disposable PostgreSQL service provisioned by CI",
)
def test_upgrade_refuses_reserved_schema_before_mutating_any_migration_objects():
    """Exercise upgrade() in a rolled-back PostgreSQL transaction, not the helper alone."""
    import pytest
    from alembic.migration import MigrationContext
    from alembic.operations import Operations
    from sqlalchemy import text
    from sqlalchemy.ext.asyncio import create_async_engine

    async def run():
        engine = create_async_engine(os.environ["ATLAS_POSTGRES_URL"])
        try:
            async with engine.connect() as conn:
                transaction = await conn.begin()
                try:
                    def exercise(sync_conn):
                        schema_exists = bool(sync_conn.execute(text(
                            "SELECT EXISTS (SELECT 1 FROM pg_namespace WHERE nspname = :name)"
                        ), {"name": _migration._INDEX_STATE_SCHEMA}).scalar_one())
                        preserved_rows = None
                        if schema_exists:
                            sync_conn.exec_driver_sql(
                                'ALTER SCHEMA "atlas_migration_meta" RENAME TO "atlas_migration_meta_test_preserved"'
                            )
                            preserved_table_exists = bool(sync_conn.execute(text("""
                                SELECT EXISTS (
                                    SELECT 1 FROM pg_class c
                                    JOIN pg_namespace n ON n.oid = c.relnamespace
                                    WHERE n.nspname = 'atlas_migration_meta_test_preserved'
                                      AND c.relname = 'schema_metadata_reconciliation_0052_index_state'
                                )
                            """)).scalar_one())
                            if preserved_table_exists:
                                preserved_rows = int(sync_conn.exec_driver_sql(
                                    'SELECT COUNT(*) FROM "atlas_migration_meta_test_preserved"."schema_metadata_reconciliation_0052_index_state"'
                                ).scalar_one())

                        sync_conn.exec_driver_sql('CREATE SCHEMA "atlas_migration_meta"')
                        sync_conn.exec_driver_sql(
                            'CREATE TABLE "atlas_migration_meta"."sentinel" '
                            '(id integer PRIMARY KEY, marker text NOT NULL)'
                        )
                        sync_conn.exec_driver_sql(
                            'INSERT INTO "atlas_migration_meta"."sentinel" (id, marker) VALUES (1, \'do-not-touch\')'
                        )
                        sync_conn.exec_driver_sql("""
                            CREATE TEMP TABLE trading_accounts (
                                pilot_status text,
                                pilot_stage integer,
                                pilot_requested_by text,
                                pilot_approved_by text,
                                pilot_max_position_notional_usd numeric,
                                pilot_max_total_exposure_usd numeric,
                                pilot_max_open_positions integer,
                                pilot_max_leverage double precision,
                                pilot_daily_loss_limit double precision
                            )
                        """)
                        sync_conn.exec_driver_sql("""
                            INSERT INTO pg_temp.trading_accounts VALUES
                            (NULL, NULL, NULL, NULL, NULL, NULL, NULL, NULL, NULL)
                        """)

                        with Operations.context(MigrationContext.configure(sync_conn)):
                            with pytest.raises(
                                RuntimeError,
                                match="reserved migration schema atlas_migration_meta already exists",
                            ):
                                _migration.upgrade()

                        sentinel = sync_conn.exec_driver_sql(
                            'SELECT marker FROM "atlas_migration_meta"."sentinel" WHERE id = 1'
                        ).scalar_one()
                        assert sentinel == "do-not-touch"
                        mutation_count = sync_conn.exec_driver_sql("""
                            SELECT
                                (pilot_status IS NULL)::int +
                                (pilot_stage IS NULL)::int +
                                (pilot_requested_by IS NULL)::int +
                                (pilot_approved_by IS NULL)::int +
                                (pilot_max_position_notional_usd IS NULL)::int +
                                (pilot_max_total_exposure_usd IS NULL)::int +
                                (pilot_max_open_positions IS NULL)::int +
                                (pilot_max_leverage IS NULL)::int +
                                (pilot_daily_loss_limit IS NULL)::int
                            FROM pg_temp.trading_accounts
                        """).scalar_one()
                        assert mutation_count == 9, "upgrade modified trading_accounts before refusal"
                        provenance_created = sync_conn.execute(text("""
                            SELECT EXISTS (
                                SELECT 1 FROM pg_class c
                                JOIN pg_namespace n ON n.oid = c.relnamespace
                                WHERE n.nspname = 'atlas_migration_meta'
                                  AND c.relname = 'schema_metadata_reconciliation_0052_index_state'
                            )
                        """)).scalar_one()
                        assert not provenance_created, "refused upgrade created a migration-owned provenance table"

                        if schema_exists:
                            assert sync_conn.execute(text("""
                                SELECT EXISTS (
                                    SELECT 1 FROM pg_namespace WHERE nspname = 'atlas_migration_meta_test_preserved'
                                )
                            """)).scalar_one()
                            if preserved_rows is not None:
                                assert int(sync_conn.exec_driver_sql(
                                    'SELECT COUNT(*) FROM "atlas_migration_meta_test_preserved"."schema_metadata_reconciliation_0052_index_state"'
                                ).scalar_one()) == preserved_rows

                    await conn.run_sync(exercise)
                finally:
                    await transaction.rollback()
        finally:
            await engine.dispose()

    asyncio.run(run())


@pytest.mark.postgres
@pytest.mark.skipif(
    not os.getenv("ATLAS_POSTGRES_URL"),
    reason="requires the disposable PostgreSQL service provisioned by CI",
)
def test_index_provenance_lookup_and_comment_restore_are_schema_scoped():
    """Same-named indexes in separate schemas must never exchange definitions/comments."""
    from alembic.migration import MigrationContext
    from alembic.operations import Operations
    from sqlalchemy import text
    from sqlalchemy.ext.asyncio import create_async_engine

    async def run():
        engine = create_async_engine(os.environ["ATLAS_POSTGRES_URL"])
        try:
            async with engine.connect() as conn:
                transaction = await conn.begin()
                try:
                    def exercise(sync_conn):
                        sync_conn.exec_driver_sql('CREATE SCHEMA "atlas_index_scope_test"')
                        sync_conn.exec_driver_sql(
                            'CREATE TABLE "atlas_index_scope_test"."target_table" (private_value integer)'
                        )
                        sync_conn.exec_driver_sql(
                            'CREATE INDEX "atlas_shared_index" ON "atlas_index_scope_test"."target_table" (private_value)'
                        )
                        sync_conn.exec_driver_sql(
                            'COMMENT ON INDEX "atlas_index_scope_test"."atlas_shared_index" IS \'target-comment\''
                        )
                        sync_conn.exec_driver_sql(
                            'CREATE TABLE public.atlas_index_scope_other (public_value text)'
                        )
                        sync_conn.exec_driver_sql(
                            'CREATE INDEX "atlas_shared_index" ON public.atlas_index_scope_other (public_value)'
                        )
                        sync_conn.exec_driver_sql(
                            'COMMENT ON INDEX public.atlas_shared_index IS \'other-schema-comment\''
                        )

                        info = _migration._visible_index_definition(
                            sync_conn, "target_table", "atlas_shared_index", "atlas_index_scope_test"
                        )
                        assert info is not None
                        assert "private_value" in info["definition"]
                        assert info["comment"] == "target-comment"

                        sync_conn.exec_driver_sql(
                            'DROP INDEX "atlas_index_scope_test"."atlas_shared_index"'
                        )
                        with Operations.context(MigrationContext.configure(sync_conn)):
                            _migration._restore_index_definition(
                                sync_conn, "target_table", "atlas_shared_index",
                                info["definition"], info["comment"], "atlas_index_scope_test",
                            )

                        restored = _migration._visible_index_definition(
                            sync_conn, "target_table", "atlas_shared_index", "atlas_index_scope_test"
                        )
                        other = sync_conn.execute(text("""
                            SELECT obj_description(idx.oid, 'pg_class')
                            FROM pg_class idx
                            JOIN pg_namespace ns ON ns.oid = idx.relnamespace
                            WHERE ns.nspname = 'public' AND idx.relname = 'atlas_shared_index'
                        """)).scalar_one()
                        assert restored["comment"] == "target-comment"
                        assert other == "other-schema-comment"

                    await conn.run_sync(exercise)
                finally:
                    await transaction.rollback()
        finally:
            await engine.dispose()

    asyncio.run(run())
