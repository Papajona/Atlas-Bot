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
        sa.Column("claim_token", sa.String(length=36), nullable=True),
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
    # Keep this service-only outbox behind the public-schema RLS boundary.
    # No API role or PUBLIC receives access. Only the explicit atlas_runtime
    # backend identity, which can already append audit_log rows, gets outbox CRUD.
    if op.get_bind().dialect.name == "postgresql":
        op.execute(
            "ALTER TABLE public.audit_log_external_delivery ENABLE ROW LEVEL SECURITY"
        )
        op.execute(
            "REVOKE ALL PRIVILEGES ON TABLE public.audit_log_external_delivery FROM PUBLIC"
        )
        op.execute(
            """
            DO $atlas_audit_delivery_acl$
            DECLARE role_name text;
            BEGIN
                FOREACH role_name IN ARRAY ARRAY['anon', 'authenticated'] LOOP
                    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = role_name) THEN
                        EXECUTE format(
                            'REVOKE ALL PRIVILEGES ON TABLE public.audit_log_external_delivery FROM %I',
                            role_name
                        );
                    END IF;
                END LOOP;
                FOR role_name IN
                    SELECT r.rolname
                    FROM pg_roles r
                    WHERE r.rolcanlogin AND r.rolbypassrls
                      AND NOT r.rolsuper AND NOT r.rolcreaterole AND NOT r.rolcreatedb
                      AND NOT r.rolreplication AND NOT r.rolinherit
                      AND NOT EXISTS (
                        SELECT 1 FROM pg_auth_members m WHERE m.member = r.oid
                      )
                      AND r.rolname NOT IN (
                          'postgres', 'service_role', 'supabase_admin',
                          'supabase_auth_admin', 'authenticator', 'anon', 'authenticated'
                      )
                      AND has_table_privilege(r.rolname, 'public.audit_log', 'INSERT')
                LOOP
                    EXECUTE format(
                        'GRANT SELECT, INSERT, UPDATE ON TABLE public.audit_log_external_delivery TO %I',
                        role_name
                    );
                END LOOP;
            END
            $atlas_audit_delivery_acl$;
            """
        )


def downgrade() -> None:
    op.drop_index(
        "ix_audit_log_external_delivery_status_lease",
        table_name="audit_log_external_delivery",
    )
    op.drop_table("audit_log_external_delivery")
