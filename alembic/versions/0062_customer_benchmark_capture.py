"""Store per-request opt-in Native AI benchmark captures with private ACLs and TTL.

Revision ID: 0062_customer_benchmark_capture
Revises: 0061_strategy_outcome_fill_aggregation
"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "0062_customer_benchmark_capture"
down_revision = "0061_strategy_outcome_fill_aggregation"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "customer_benchmark_captures",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column(
            "customer_id",
            sa.Integer(),
            sa.ForeignKey("customer_profiles.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("prompt_ciphertext", sa.Text(), nullable=False),
        sa.Column("native_output_ciphertext", sa.Text(), nullable=False),
        sa.Column("prompt_sha256", sa.String(64), nullable=False),
        sa.Column("consent_version", sa.String(80), nullable=False),
        sa.Column("consented_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("asset", sa.String(20), nullable=False),
        sa.Column("symbol", sa.String(80), nullable=False),
        sa.Column("exchange", sa.String(50), nullable=False),
        sa.Column("timeframe", sa.String(20), nullable=False),
        sa.Column("native_ai_version", sa.String(80), nullable=False, server_default=""),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("expires_at > created_at", name="ck_benchmark_capture_expiry_after_create"),
    )
    op.create_index(
        "ix_benchmark_capture_customer_created",
        "customer_benchmark_captures",
        ["customer_id", "created_at"],
    )
    op.create_index(
        "ix_benchmark_capture_expires_at",
        "customer_benchmark_captures",
        ["expires_at"],
    )

    bind = op.get_bind()
    if bind.dialect.name != "postgresql":
        return

    op.execute(sa.text(
        "ALTER TABLE public.customer_benchmark_captures ENABLE ROW LEVEL SECURITY"
    ))
    op.execute(sa.text(
        "REVOKE ALL PRIVILEGES ON TABLE public.customer_benchmark_captures FROM PUBLIC"
    ))
    op.execute(sa.text("""
        DO $atlas_benchmark_runtime_acl$
        BEGIN
            IF EXISTS (
                SELECT 1 FROM pg_roles WHERE rolname = 'atlas_api_runtime'
            ) THEN
                EXECUTE 'GRANT SELECT, INSERT, DELETE ON TABLE public.customer_benchmark_captures TO atlas_api_runtime';
                EXECUTE 'GRANT USAGE, SELECT ON SEQUENCE public.customer_benchmark_captures_id_seq TO atlas_api_runtime';
            END IF;
        END
        $atlas_benchmark_runtime_acl$;
    """))
    op.execute(sa.text("""
        DO $atlas_benchmark_acl$
        DECLARE role_name text;
        BEGIN
            FOREACH role_name IN ARRAY ARRAY['anon', 'authenticated', 'service_role'] LOOP
                IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = role_name) THEN
                    EXECUTE format(
                        'REVOKE ALL PRIVILEGES ON TABLE public.customer_benchmark_captures FROM %I',
                        role_name
                    );
                END IF;
            END LOOP;
        END
        $atlas_benchmark_acl$;
    """))

    # Retention is handled by the monitored application worker. Do not
    # make schema upgrades depend on managed pg_cron flags or privileges.

def downgrade() -> None:
    # No database scheduler is created by this revision; the application worker
    # is stopped by normal process shutdown and needs no migration rollback.
    op.drop_index("ix_benchmark_capture_expires_at", table_name="customer_benchmark_captures")
    op.drop_index("ix_benchmark_capture_customer_created", table_name="customer_benchmark_captures")
    op.drop_table("customer_benchmark_captures")
