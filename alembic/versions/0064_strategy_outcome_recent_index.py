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
    existing = {
        item["name"]
        for item in sa.inspect(bind).get_indexes("strategy_outcomes", schema=schema)
    }
    options = (
        {"postgresql_where": _PREDICATE}
        if bind.dialect.name == "postgresql"
        else {"sqlite_where": _PREDICATE}
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
    if _TRADE_INDEX_NAME not in existing:
        op.create_index(
            _TRADE_INDEX_NAME,
            "strategy_outcomes",
            ["trade_id", "created_at", "id"],
            unique=False,
            schema=schema,
            **options,
        )


def downgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name not in {"postgresql", "sqlite"}:
        return
    schema = "public" if bind.dialect.name == "postgresql" else None
    existing = {
        item["name"]
        for item in sa.inspect(bind).get_indexes("strategy_outcomes", schema=schema)
    }
    if _TRADE_INDEX_NAME in existing:
        op.drop_index(_TRADE_INDEX_NAME, table_name="strategy_outcomes", schema=schema)
    if _INDEX_NAME in existing:
        op.drop_index(_INDEX_NAME, table_name="strategy_outcomes", schema=schema)
