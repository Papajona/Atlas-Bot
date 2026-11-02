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
    context = op.get_context()
    if context.dialect.name == "postgresql":
        bind = op.get_bind()
        # An autocommit block commits the column addition even if the following
        # concurrent index build fails. On retry, detect the committed column
        # instead of attempting to add it a second time.
        if not sa.inspect(bind).has_column("audit_log", "idempotency_key"):
            op.add_column(
                "audit_log",
                sa.Column("idempotency_key", sa.String(length=64), nullable=True),
            )

        # PostgreSQL can leave an invalid index after a failed CREATE INDEX
        # CONCURRENTLY. Reuse a valid index; remove an invalid remnant before
        # retrying the build. The catalog query is scoped to the current schema.
        index_state = bind.execute(
            sa.text(
                """
                SELECT i.indisvalid, i.indisready
                FROM pg_class AS index_class
                JOIN pg_namespace AS namespace
                  ON namespace.oid = index_class.relnamespace
                JOIN pg_index AS i
                  ON i.indexrelid = index_class.oid
                WHERE namespace.nspname = current_schema()
                  AND index_class.relname = 'uq_audit_log_event_idempotency'
                """
            )
        ).first()
        index_is_valid = bool(index_state and index_state[0] and index_state[1])
        index_exists = index_state is not None
        with context.autocommit_block():
            if index_exists and not index_is_valid:
                op.execute(
                    sa.text(
                        "DROP INDEX CONCURRENTLY IF EXISTS uq_audit_log_event_idempotency"
                    )
                )
            if not index_is_valid:
                op.create_index(
                    "uq_audit_log_event_idempotency",
                    "audit_log",
                    ["event", "idempotency_key"],
                    unique=True,
                    postgresql_concurrently=True,
                )
    else:
        if not sa.inspect(op.get_bind()).has_column("audit_log", "idempotency_key"):
            op.add_column(
                "audit_log",
                sa.Column("idempotency_key", sa.String(length=64), nullable=True),
            )
        if not sa.inspect(op.get_bind()).has_index("uq_audit_log_event_idempotency", "audit_log"):
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
