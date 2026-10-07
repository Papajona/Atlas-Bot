"""Add atomic AI credit usage tracking to subscriptions.

Revision ID: 0046_subscription_ai_usage
Revises: 0045_merge_application_heads
"""
from alembic import op
import sqlalchemy as sa

revision = "0046_subscription_ai_usage"
down_revision = "0045_merge_application_heads"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("subscriptions", sa.Column("ai_credits_used", sa.Integer(), nullable=False, server_default="0"))
    op.add_column("subscriptions", sa.Column("ai_usage_period_start", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("CURRENT_TIMESTAMP")))


def downgrade():
    op.drop_column("subscriptions", "ai_usage_period_start")
    op.drop_column("subscriptions", "ai_credits_used")
