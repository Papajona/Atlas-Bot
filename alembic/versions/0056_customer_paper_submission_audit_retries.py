"""Add indexed durable retry state for customer paper submission audit delivery.

Revision ID: 0056_customer_paper_submission_audit_retries
Revises: 0055_trading_account_customer_fk
"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa

revision = "0056_customer_paper_submission_audit_retries"
down_revision = "0055_trading_account_customer_fk"
branch_labels = None
depends_on = None

TABLE = "customer_paper_submission_audit_retries"
INDEX = "ix_customer_paper_submission_audit_retry_pending"


def upgrade() -> None:
    op.create_table(
        TABLE,
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("customer_id", sa.Integer(), nullable=False),
        sa.Column("client_order_id", sa.String(length=120), nullable=False),
        sa.Column("submission_detail", sa.Text(), nullable=False),
        sa.Column("status", sa.String(length=20), nullable=False, server_default="PENDING"),
        sa.Column("attempt_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("last_error_type", sa.String(length=80), nullable=False, server_default=""),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.ForeignKeyConstraint(
            ["customer_id"],
            ["customer_profiles.id"],
            name="fk_customer_paper_audit_retry_customer",
            ondelete="CASCADE",
        ),
        sa.UniqueConstraint(
            "customer_id",
            "client_order_id",
            name="uq_customer_paper_submission_audit_retry_order",
        ),
    )
    op.create_index(
        INDEX,
        TABLE,
        ["customer_id", "status", "updated_at", "id"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index(INDEX, table_name=TABLE)
    op.drop_table(TABLE)
