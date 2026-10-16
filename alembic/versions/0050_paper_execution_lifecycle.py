"""Persist platform paper order lifecycle, bar budgets, and idempotent fill attempts.

Revision ID: 0050_paper_execution_lifecycle
Revises: 0049_public_schema_rls_lockdown
"""
from alembic import op
import sqlalchemy as sa

revision = "0050_paper_execution_lifecycle"
down_revision = "0049_public_schema_rls_lockdown"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "paper_order_states",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("trade_id", sa.Integer(), sa.ForeignKey("trades.id", ondelete="CASCADE"), nullable=False),
        sa.Column("exchange", sa.String(50), nullable=False),
        sa.Column("symbol", sa.String(80), nullable=False),
        sa.Column("timeframe", sa.String(20), nullable=False),
        sa.Column("submitted_bar_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_processed_bar_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("bars_elapsed", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("status", sa.String(20), nullable=False, server_default="OPEN"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.UniqueConstraint("trade_id", name="uq_paper_order_state_trade"),
    )
    op.create_index("ix_paper_order_state_market", "paper_order_states", ["exchange", "symbol", "timeframe", "status"])

    op.create_table(
        "paper_bar_states",
        sa.Column("bar_key", sa.String(64), primary_key=True),
        sa.Column("exchange", sa.String(50), nullable=False),
        sa.Column("symbol", sa.String(80), nullable=False),
        sa.Column("timeframe", sa.String(20), nullable=False),
        sa.Column("bar_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("asset_class", sa.String(20), nullable=False, server_default="unknown"),
        sa.Column("data_source", sa.String(50), nullable=False, server_default=""),
        sa.Column("data_sha256", sa.String(64), nullable=False, server_default=""),
        sa.Column("volume_unit", sa.String(30), nullable=False, server_default="unknown"),
        sa.Column("bar_volume", sa.Numeric(38, 18), nullable=False, server_default="0"),
        sa.Column("capacity", sa.Numeric(38, 18), nullable=False, server_default="0"),
        sa.Column("consumed", sa.Numeric(38, 18), nullable=False, server_default="0"),
        sa.Column("close_price", sa.Numeric(38, 18), nullable=False, server_default="0"),
        sa.Column("return_volatility", sa.Float(), nullable=False, server_default="0"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.UniqueConstraint("exchange", "symbol", "timeframe", "bar_at", name="uq_paper_bar_market_time"),
    )
    op.create_index("ix_paper_bar_market_time", "paper_bar_states", ["exchange", "symbol", "timeframe", "bar_at"])

    op.create_table(
        "paper_fill_events",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("trade_id", sa.Integer(), sa.ForeignKey("trades.id", ondelete="CASCADE"), nullable=False),
        sa.Column("bar_key", sa.String(64), sa.ForeignKey("paper_bar_states.bar_key", ondelete="CASCADE"), nullable=False),
        sa.Column("outcome", sa.String(30), nullable=False),
        sa.Column("attempted_quantity", sa.Numeric(38, 18), nullable=False, server_default="0"),
        sa.Column("filled_quantity", sa.Numeric(38, 18), nullable=False, server_default="0"),
        sa.Column("fill_price", sa.Numeric(38, 18), nullable=False, server_default="0"),
        sa.Column("fee", sa.Numeric(38, 18), nullable=False, server_default="0"),
        sa.Column("slippage_bps", sa.Float(), nullable=False, server_default="0"),
        sa.Column("reason", sa.Text(), nullable=False, server_default=""),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.UniqueConstraint("trade_id", "bar_key", name="uq_paper_fill_trade_bar"),
    )
    op.create_index("ix_paper_fill_bar", "paper_fill_events", ["bar_key"])

    # These are server-owned execution records, not API tables. No generic policies
    # are created. RLS and direct API-role revokes make the existing trusted-server
    # application route the only intended access path.
    for table in ("paper_order_states", "paper_bar_states", "paper_fill_events"):
        op.execute(f'ALTER TABLE public.{table} ENABLE ROW LEVEL SECURITY')
        op.execute(f'REVOKE ALL PRIVILEGES ON TABLE public.{table} FROM PUBLIC')
    op.execute(
        """
        DO $paper_rls$
        DECLARE role_name text;
        BEGIN
            FOREACH role_name IN ARRAY ARRAY['anon', 'authenticated'] LOOP
                IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = role_name) THEN
                    EXECUTE format('REVOKE ALL PRIVILEGES ON TABLE public.paper_order_states FROM %I', role_name);
                    EXECUTE format('REVOKE ALL PRIVILEGES ON TABLE public.paper_bar_states FROM %I', role_name);
                    EXECUTE format('REVOKE ALL PRIVILEGES ON TABLE public.paper_fill_events FROM %I', role_name);
                END IF;
            END LOOP;
        END
        $paper_rls$;
        """
    )


def downgrade():
    op.drop_index("ix_paper_fill_bar", table_name="paper_fill_events")
    op.drop_table("paper_fill_events")
    op.drop_index("ix_paper_bar_market_time", table_name="paper_bar_states")
    op.drop_table("paper_bar_states")
    op.drop_index("ix_paper_order_state_market", table_name="paper_order_states")
    op.drop_table("paper_order_states")
