"""Regression tests for Alembic 0052 index-state downgrade behavior."""
import importlib.util
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
MIGRATION_PATH = ROOT / "alembic" / "versions" / "0052_schema_metadata_reconciliation.py"
_SPEC = importlib.util.spec_from_file_location("migration_0052_schema_metadata_reconciliation", MIGRATION_PATH)
assert _SPEC is not None and _SPEC.loader is not None
_migration = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(_migration)


def test_downgrade_drops_customer_indexes_created_when_both_names_were_absent(monkeypatch):
    """Both the historical and canonical names were absent before upgrade."""
    newly_created = {
        ("adaptive_trading_bots", "ix_adaptive_trading_bots_strategy_candidate_id"),
        ("order_commands", "ix_order_commands_customer_id"),
    }
    initial = set(newly_created)

    def index_names(_bind, table):
        return {name for index_table, name in newly_created if index_table == table}

    def drop_index(_bind, table, name):
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
        lambda _bind, table: {name for index_table, name in current if index_table == table},
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
