"""Add indexed audit idempotency keys for retry-safe submission events.

Revision ID: 0054_audit_log_idempotency_key
Revises: 0053_supabase_public_default_acl_lockdown
"""
from alembic import op
import sqlalchemy as sa

revision = "0054_audit_log_idempotency_key"
down_revision = "0053_supabase_public_default_acl_lockdown"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column(
        "audit_log",
        sa.Column("idempotency_key", sa.String(length=64), nullable=True),
    )
    context = op.get_context()
    if context.dialect.name == "postgresql":
        # A normal CREATE UNIQUE INDEX takes a SHARE lock that blocks audit inserts.
        # Concurrent construction keeps append_audit/record_audit writers available.
        with context.autocommit_block():
            op.create_index(
                "uq_audit_log_event_idempotency",
                "audit_log",
                ["event", "idempotency_key"],
                unique=True,
                postgresql_concurrently=True,
            )
    else:
        op.create_index(
            "uq_audit_log_event_idempotency",
            "audit_log",
            ["event", "idempotency_key"],
            unique=True,
        )


def downgrade():
    context = op.get_context()
    if context.dialect.name == "postgresql":
        with context.autocommit_block():
            op.drop_index(
                "uq_audit_log_event_idempotency",
                table_name="audit_log",
                postgresql_concurrently=True,
            )
    else:
        op.drop_index("uq_audit_log_event_idempotency", table_name="audit_log")
    op.drop_column("audit_log", "idempotency_key")
