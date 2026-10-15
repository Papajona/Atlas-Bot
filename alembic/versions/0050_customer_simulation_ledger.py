"""Create an isolated, virtual-only customer simulation ledger.

Revision ID: 0050_customer_simulation_ledger
Revises: 0049_public_schema_rls_lockdown
"""
from alembic import op
import sqlalchemy as sa

revision = "0050_customer_simulation_ledger"
down_revision = "0049_public_schema_rls_lockdown"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "customer_simulation_accounts",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column("customer_id", sa.BigInteger(), nullable=False),
        sa.Column("asset_class", sa.String(20), nullable=False),
        sa.Column("currency", sa.String(20), nullable=False, server_default="USD"),
        sa.Column("virtual_balance", sa.Numeric(38, 18), nullable=False, server_default="0"),
        sa.Column("initial_virtual_balance", sa.Numeric(38, 18), nullable=False, server_default="0"),
        sa.Column("daily_loss_limit", sa.Numeric(38, 18), nullable=False, server_default="0"),
        sa.Column("status", sa.String(20), nullable=False, server_default="ACTIVE"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.UniqueConstraint("customer_id", "asset_class", "currency", name="uq_sim_account_customer_asset_currency"),
        sa.UniqueConstraint("id", "customer_id", name="uq_sim_account_id_customer"),
        sa.CheckConstraint("asset_class IN ('crypto', 'forex', 'commodity')", name="ck_sim_account_asset_class"),
        sa.CheckConstraint("virtual_balance >= 0", name="ck_sim_account_nonnegative_balance"),
        sa.CheckConstraint("initial_virtual_balance >= 0", name="ck_sim_account_nonnegative_initial"),
        sa.CheckConstraint("daily_loss_limit >= 0", name="ck_sim_account_nonnegative_daily_loss_limit"),
        sa.CheckConstraint("status IN ('ACTIVE', 'SUSPENDED', 'CLOSED')", name="ck_sim_account_status"),
    )
    op.create_index("ix_sim_account_customer_status", "customer_simulation_accounts", ["customer_id", "status"])

    op.create_table(
        "customer_simulation_events",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column("account_id", sa.BigInteger(), nullable=False),
        sa.Column("customer_id", sa.BigInteger(), nullable=False),
        sa.Column("idempotency_key", sa.String(180), nullable=False),
        sa.Column("event_type", sa.String(30), nullable=False),
        sa.Column("asset_class", sa.String(20), nullable=False),
        sa.Column("symbol", sa.String(80), nullable=False, server_default=""),
        sa.Column("quantity", sa.Numeric(38, 18), nullable=False, server_default="0"),
        sa.Column("price", sa.Numeric(38, 18), nullable=False, server_default="0"),
        sa.Column("balance_delta", sa.Numeric(38, 18), nullable=False),
        sa.Column("balance_after", sa.Numeric(38, 18), nullable=False),
        sa.Column("metadata_json", sa.Text(), nullable=False, server_default="{}"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.ForeignKeyConstraint(["account_id", "customer_id"], ["customer_simulation_accounts.id", "customer_simulation_accounts.customer_id"], name="fk_sim_event_account_customer"),
        sa.UniqueConstraint("account_id", "idempotency_key", name="uq_sim_event_idempotency"),
        sa.CheckConstraint("event_type IN ('VIRTUAL_SEED', 'ORDER_FILL', 'REALIZED_PNL', 'FEE', 'ORDER_CANCEL', 'STOP_LOSS', 'TAKE_PROFIT', 'ADJUSTMENT')", name="ck_sim_event_type"),
        sa.CheckConstraint("asset_class IN ('crypto', 'forex', 'commodity')", name="ck_sim_event_asset_class"),
        sa.CheckConstraint("quantity >= 0 AND price >= 0", name="ck_sim_event_nonnegative_qty_price"),
        sa.CheckConstraint("balance_after >= 0", name="ck_sim_event_nonnegative_balance_after"),
    )
    op.create_index("ix_sim_event_customer_created", "customer_simulation_events", ["customer_id", "created_at"])
    op.create_index("ix_sim_event_account_created", "customer_simulation_events", ["account_id", "created_at"])

    # Match the repository's public-schema lockdown: no generic client policy.
    # Service-role access must be granted only through the application's DB role.
    op.execute("ALTER TABLE public.customer_simulation_accounts ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE public.customer_simulation_events ENABLE ROW LEVEL SECURITY")


def downgrade() -> None:
    op.drop_index("ix_sim_event_account_created", table_name="customer_simulation_events")
    op.drop_index("ix_sim_event_customer_created", table_name="customer_simulation_events")
    op.drop_table("customer_simulation_events")
    op.drop_index("ix_sim_account_customer_status", table_name="customer_simulation_accounts")
    op.drop_table("customer_simulation_accounts")
