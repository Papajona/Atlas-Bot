"""Index recent platform close rows for bounded risk-dashboard sampling."""
from alembic import op
import sqlalchemy as sa


revision = "0064_strategy_outcome_recent_index"
down_revision = "0063_webhook_events_created_at_index"
branch_labels = None
depends_on = None

_INDEX_NAME = "ix_strategy_outcome_platform_closed_recent"
_TRADE_INDEX_NAME = "ix_strategy_outcome_platform_closed_trade_time"
_PREDICATE = sa.text("customer_id IS NULL AND lifecycle_status = 'CLOSED'")


def upgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name not in {"postgresql", "sqlite"}:
        return
    schema = "public" if bind.dialect.name == "postgresql" else None
    indexes = {
        item["name"]: item
        for item in sa.inspect(bind).get_indexes("strategy_outcomes", schema=schema)
    }
    existing_trade_index = indexes.get(_TRADE_INDEX_NAME)
    expected_trade_columns = ["trade_id", "created_at", "id"]
    if (
        existing_trade_index is not None
        and list(existing_trade_index.get("column_names") or []) != expected_trade_columns
    ):
        # Revision 0060 owns a two-column index with this name. Replace it here
        # so the per-trade risk query's deterministic id tie-breaker is indexed.
        op.drop_index(_TRADE_INDEX_NAME, table_name="strategy_outcomes", schema=schema)
        indexes.pop(_TRADE_INDEX_NAME, None)
    existing = set(indexes)
    options = (
        {"postgresql_where": _PREDICATE}
        if bind.dialect.name == "postgresql"
        else {"sqlite_where": _PREDICATE}
    )
    if _TRADE_INDEX_NAME not in existing:
        op.create_index(
            _TRADE_INDEX_NAME,
            "strategy_outcomes",
            ["trade_id", "created_at", "id"],
            unique=False,
            schema=schema,
            **options,
        )
    if _INDEX_NAME not in existing:
        op.create_index(
            _INDEX_NAME,
            "strategy_outcomes",
            ["created_at", "id", "trade_id"],
            unique=False,
            schema=schema,
            **options,
        )


def downgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name not in {"postgresql", "sqlite"}:
        return
    schema = "public" if bind.dialect.name == "postgresql" else None
    indexes = {
        item["name"]: item
        for item in sa.inspect(bind).get_indexes("strategy_outcomes", schema=schema)
    }
    trade_index = indexes.get(_TRADE_INDEX_NAME)
    baseline_trade_columns = ["trade_id", "created_at"]
    if trade_index is not None and list(trade_index.get("column_names") or []) != baseline_trade_columns:
        op.drop_index(_TRADE_INDEX_NAME, table_name="strategy_outcomes", schema=schema)
        trade_index = None
    if trade_index is None:
        # Restore the exact two-column index owned by revision 0060; downgrading
        # this revision must not remove an index required by the remaining chain.
        options = (
            {"postgresql_where": _PREDICATE}
            if bind.dialect.name == "postgresql"
            else {"sqlite_where": _PREDICATE}
        )
        op.create_index(
            _TRADE_INDEX_NAME,
            "strategy_outcomes",
            baseline_trade_columns,
            unique=False,
            schema=schema,
            **options,
        )
    if _INDEX_NAME in indexes:
        op.drop_index(_INDEX_NAME, table_name="strategy_outcomes", schema=schema)
