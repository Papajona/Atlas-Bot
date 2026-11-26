"""Add append-only customer equity observations and account-synced display preferences.

Revision ID: 0069_customer_equity_history_and_workspace_preferences
Revises: 0068_tron_sweep_txid_casefold_unique
"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "0069_customer_equity_history_and_workspace_preferences"
down_revision = "0068_tron_sweep_txid_casefold_unique"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "customer_equity_snapshots",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column(
            "customer_id",
            sa.Integer(),
            sa.ForeignKey("customer_profiles.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("bucket_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("observed_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("equity", sa.Numeric(38, 18), nullable=False),
        sa.Column("cash_equity", sa.Numeric(38, 18), nullable=True),
        sa.Column("currency", sa.String(20), nullable=False, server_default="USDT"),
        sa.Column("valuation_source", sa.String(40), nullable=False, server_default="TRADING_ACCOUNT_EQUITY"),
        sa.UniqueConstraint(
            "customer_id", "bucket_at",
            name="uq_customer_equity_snapshot_customer_bucket",
        ),
    )
    op.create_index(
        "ix_customer_equity_snapshot_customer_observed",
        "customer_equity_snapshots",
        ["customer_id", "observed_at"],
    )
    op.create_table(
        "customer_workspace_preferences",
        sa.Column(
            "customer_id",
            sa.Integer(),
            sa.ForeignKey("customer_profiles.id", ondelete="CASCADE"),
            primary_key=True,
        ),
        sa.Column("reduce_motion", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("compact_markets", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("touch_feedback", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("mobile_default_timeframe_days", sa.Integer(), nullable=False, server_default="30"),
        sa.Column("mobile_chart_crosshair_enabled", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.CheckConstraint(
            "mobile_default_timeframe_days IN (7, 30)",
            name="ck_customer_workspace_mobile_timeframe",
        ),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
    )

    bind = op.get_bind()
    if bind.dialect.name != "postgresql":
        return

    for table_name in ("customer_equity_snapshots", "customer_workspace_preferences"):
        op.execute(sa.text(
            f"ALTER TABLE public.{table_name} ENABLE ROW LEVEL SECURITY"
        ))
        op.execute(sa.text(
            f"ALTER TABLE public.{table_name} FORCE ROW LEVEL SECURITY"
        ))
        op.execute(sa.text(
            f"REVOKE ALL PRIVILEGES ON TABLE public.{table_name} FROM PUBLIC"
        ))

    op.execute(sa.text("""
        DO $atlas_customer_equity_preferences_acl$
        DECLARE
            role_name text;
        BEGIN
            FOREACH role_name IN ARRAY ARRAY['anon', 'authenticated', 'service_role'] LOOP
                IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = role_name) THEN
                    EXECUTE format(
                        'REVOKE ALL PRIVILEGES ON TABLE public.customer_equity_snapshots FROM %I',
                        role_name
                    );
                    EXECUTE format(
                        'REVOKE ALL PRIVILEGES ON TABLE public.customer_workspace_preferences FROM %I',
                        role_name
                    );
                END IF;
            END LOOP;

            IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'atlas_api_runtime') THEN
                EXECUTE
                    'GRANT SELECT, INSERT ON TABLE public.customer_equity_snapshots TO atlas_api_runtime';
                EXECUTE
                    'GRANT SELECT, INSERT, UPDATE ON TABLE public.customer_workspace_preferences TO atlas_api_runtime';
                EXECUTE
                    'GRANT USAGE, SELECT ON SEQUENCE public.customer_equity_snapshots_id_seq TO atlas_api_runtime';
            END IF;
        END
        $atlas_customer_equity_preferences_acl$;
    """))


def downgrade() -> None:
    raise RuntimeError(
        "0069 stores customer equity history; destructive downgrade is prohibited. "
        "Use a forward migration after an explicit retention decision."
    )
