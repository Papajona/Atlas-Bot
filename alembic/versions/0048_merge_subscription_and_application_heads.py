"""Merge the application and subscription-AI-usage migration heads.

This merge revision deliberately performs no schema changes. It joins the
existing application/custody head with the subscription AI-usage/outcome
lifecycle head without rewriting historical migrations.

Revision ID: 0048_merge_subscription_and_application_heads
Revises: 0045_merge_application_heads, 0047_strategy_outcome_lifecycle
"""
from alembic import op

revision = "0048_merge_subscription_and_application_heads"
down_revision = (
    "0045_merge_application_heads",
    "0047_strategy_outcome_lifecycle",
)
branch_labels = None
depends_on = None


def upgrade() -> None:
    pass


def downgrade() -> None:
    pass
