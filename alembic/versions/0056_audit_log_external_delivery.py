"""Track at-least-once external delivery of immutable audit events.

The audit_log row remains append-only and hash chained. Delivery status lives in a
separate outbox table so retries do not mutate the protected event payload/hash.
"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa

revision = "0056_audit_log_external_delivery"
down_revision = "0055_trading_account_customer_fk"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "audit_log_external_delivery",
        sa.Column("event_hash", sa.String(length=64), primary_key=True),
        sa.Column("status", sa.String(length=20), nullable=False, server_default="PENDING"),
        sa.Column("attempt_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("lease_until", sa.DateTime(timezone=True), nullable=True),
        sa.Column("delivered_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_error_type", sa.String(length=80), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
    )
    op.create_index(
        "ix_audit_log_external_delivery_status_lease",
        "audit_log_external_delivery",
        ["status", "lease_until"],
        unique=False,
    )
    # Keep this service-only outbox behind the same public-schema RLS boundary
    # as every other public table. No anon/authenticated policy or grant is added.
    if op.get_bind().dialect.name == "postgresql":
        op.execute(
            "ALTER TABLE public.audit_log_external_delivery ENABLE ROW LEVEL SECURITY"
        )


def downgrade() -> None:
    op.drop_index(
        "ix_audit_log_external_delivery_status_lease",
        table_name="audit_log_external_delivery",
    )
    op.drop_table("audit_log_external_delivery")
