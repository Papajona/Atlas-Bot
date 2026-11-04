"""Add indexed durable state for customer paper submission audit retries.

Revision ID: 0057_customer_paper_submission_audit_retries
Revises: 0056_audit_log_external_delivery
"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa

revision = "0057_customer_paper_submission_audit_retries"
down_revision = "0056_audit_log_external_delivery"
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
    # Migrations after 0049 must explicitly enable RLS and assign access.
    if op.get_bind().dialect.name == "postgresql":
        op.execute(
            f"ALTER TABLE public.{TABLE} ENABLE ROW LEVEL SECURITY"
        )
        op.execute(
            f"REVOKE ALL PRIVILEGES ON TABLE public.{TABLE} FROM PUBLIC"
        )
        op.execute(
            "REVOKE ALL PRIVILEGES ON SEQUENCE public.customer_paper_submission_audit_retries_id_seq FROM PUBLIC"
        )
        op.execute(
            """
            DO $atlas_customer_paper_retry_acl$
            DECLARE role_name text;
            BEGIN
                FOREACH role_name IN ARRAY ARRAY['anon', 'authenticated'] LOOP
                    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = role_name) THEN
                        EXECUTE format(
                            'REVOKE ALL PRIVILEGES ON TABLE public.customer_paper_submission_audit_retries FROM %I',
                            role_name
                        );
                        EXECUTE format(
                            'REVOKE ALL PRIVILEGES ON SEQUENCE public.customer_paper_submission_audit_retries_id_seq FROM %I',
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
                          'supabase_auth_admin', 'authenticator', 'anon', 'authenticated',
                          'atlas_paper_reader'
                      )
                      AND has_table_privilege(r.rolname, 'public.audit_log', 'INSERT')
                LOOP
                    EXECUTE format(
                        'GRANT SELECT, INSERT, UPDATE ON TABLE public.customer_paper_submission_audit_retries TO %I',
                        role_name
                    );
                    EXECUTE format(
                        'GRANT USAGE, SELECT ON SEQUENCE public.customer_paper_submission_audit_retries_id_seq TO %I',
                        role_name
                    );
                END LOOP;
            END
            $atlas_customer_paper_retry_acl$;
            """
        )


def downgrade() -> None:
    op.drop_index(INDEX, table_name=TABLE)
    op.drop_table(TABLE)
