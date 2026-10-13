"""Add customer risk profiles and durable risk-decision history.

Revision ID: 0051_customer_risk_profiles_and_decisions
Revises: 0050_customer_simulation_ledger
"""
from alembic import op
import sqlalchemy as sa

revision = "0051_customer_risk_profiles_and_decisions"
down_revision = "0050_customer_simulation_ledger"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "customer_risk_profiles",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("customer_id", sa.Integer(), sa.ForeignKey("customer_profiles.id", ondelete="CASCADE"), nullable=False),
        sa.Column("daily_loss_limit_pct", sa.Numeric(8, 6), nullable=False, server_default="0.02"),
        sa.Column("updated_by", sa.String(160), nullable=False, server_default="customer"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.UniqueConstraint("customer_id", name="uq_customer_risk_profile_customer"),
        sa.CheckConstraint("daily_loss_limit_pct >= 0.001 AND daily_loss_limit_pct <= 0.15", name="ck_customer_risk_daily_loss_range"),
    )
    op.create_index("ix_customer_risk_profile_customer", "customer_risk_profiles", ["customer_id"])

    op.create_table(
        "customer_risk_decisions",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("customer_id", sa.Integer(), sa.ForeignKey("customer_profiles.id", ondelete="CASCADE"), nullable=False),
        sa.Column("signal_id", sa.String(160), nullable=False, server_default=""),
        sa.Column("symbol", sa.String(80), nullable=False),
        sa.Column("side", sa.String(10), nullable=False),
        sa.Column("requested_price", sa.Numeric(38, 18), nullable=False),
        sa.Column("quantity", sa.Numeric(38, 18), nullable=False),
        sa.Column("execution_mode", sa.String(20), nullable=False),
        sa.Column("verdict", sa.String(20), nullable=False),
        sa.Column("reason", sa.Text(), nullable=False, server_default=""),
        sa.Column("signal_timestamp", sa.String(80), nullable=False, server_default=""),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.CheckConstraint("execution_mode IN ('LIVE', 'PAPER')", name="ck_customer_risk_decision_mode"),
        sa.CheckConstraint("verdict IN ('ALLOWED', 'BLOCKED')", name="ck_customer_risk_decision_verdict"),
    )
    op.create_index("ix_customer_risk_decision_customer_created", "customer_risk_decisions", ["customer_id", "created_at"])
    op.create_index("ix_customer_risk_decision_customer_signal", "customer_risk_decisions", ["customer_id", "signal_id"])

    # Public-schema lockdown requires RLS on every new public table. Policies remain
    # absent: access is through the authenticated server-side application role.
    op.execute("ALTER TABLE public.customer_risk_profiles ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE public.customer_risk_decisions ENABLE ROW LEVEL SECURITY")


def downgrade() -> None:
    op.drop_index("ix_customer_risk_decision_customer_signal", table_name="customer_risk_decisions")
    op.drop_index("ix_customer_risk_decision_customer_created", table_name="customer_risk_decisions")
    op.drop_table("customer_risk_decisions")
    op.drop_index("ix_customer_risk_profile_customer", table_name="customer_risk_profiles")
    op.drop_table("customer_risk_profiles")
