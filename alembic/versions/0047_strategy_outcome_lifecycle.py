"""Add explicit lifecycle state to observed strategy outcomes.

Revision ID: 0047_strategy_outcome_lifecycle
Revises: 0046_subscription_ai_usage
"""

from alembic import op
import sqlalchemy as sa

revision = "0047_strategy_outcome_lifecycle"
down_revision = "0046_subscription_ai_usage"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    columns = {c["name"] for c in inspector.get_columns("strategy_outcomes")}
    if "lifecycle_status" not in columns:
        op.add_column(
            "strategy_outcomes",
            sa.Column("lifecycle_status", sa.String(20), nullable=False, server_default="OPEN"),
        )

    indexes = {i["name"] for i in inspector.get_indexes("strategy_outcomes")}
    if "ix_strategy_outcome_lifecycle_created" not in indexes:
        op.create_index(
            "ix_strategy_outcome_lifecycle_created",
            "strategy_outcomes",
            ["lifecycle_status", "created_at"],
        )

    # Only outcomes tied to a completed learning episode are promoted to CLOSED.
    # Unknown historical rows remain OPEN and cannot become adaptive routing evidence.
    bind.execute(
        sa.text(
            """
            UPDATE strategy_outcomes
            SET lifecycle_status = 'CLOSED'
            WHERE trade_id IN (
                SELECT entry_trade_id
                FROM trade_learning_episodes
                WHERE status = 'COMPLETED'
            )
            """
        )
    )


def downgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    indexes = {i["name"] for i in inspector.get_indexes("strategy_outcomes")}
    if "ix_strategy_outcome_lifecycle_created" in indexes:
        op.drop_index("ix_strategy_outcome_lifecycle_created", table_name="strategy_outcomes")
    columns = {c["name"] for c in inspector.get_columns("strategy_outcomes")}
    if "lifecycle_status" in columns:
        op.drop_column("strategy_outcomes", "lifecycle_status")
