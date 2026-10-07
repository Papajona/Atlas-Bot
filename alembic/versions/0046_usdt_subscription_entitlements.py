"""Subscription entitlements, USDT/TRC-20 payment intents, and usage ledger.

Revision ID: 0046_usdt_subscription_entitlements
Revises: 0034_withdrawal_provider_identity, 0045_merge_application_heads
"""
from alembic import op
import sqlalchemy as sa

revision = "0046_usdt_subscription_entitlements"
down_revision = ("0034_withdrawal_provider_identity", "0045_merge_application_heads")
branch_labels = None
depends_on = None


def upgrade():
    op.add_column(
        "billing_plans",
        sa.Column("monthly_trade_limit", sa.Integer(), nullable=False, server_default="0"),
    )

    op.create_table(
        "subscription_trade_usage",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("subscription_id", sa.Integer(), nullable=False),
        sa.Column("customer_id", sa.Integer(), nullable=False),
        sa.Column("period_start", sa.DateTime(timezone=True), nullable=False),
        sa.Column("period_end", sa.DateTime(timezone=True), nullable=False),
        sa.Column("trades_used", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("subscription_id", "period_start", name="uq_subscription_trade_usage_period"),
        sa.CheckConstraint("trades_used >= 0", name="ck_subscription_trade_usage_nonnegative"),
    )
    op.create_index(
        "ix_subscription_trade_usage_customer_period",
        "subscription_trade_usage",
        ["customer_id", "period_start"],
    )

    op.create_table(
        "subscription_trade_events",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("subscription_id", sa.Integer(), nullable=False),
        sa.Column("customer_id", sa.Integer(), nullable=False),
        sa.Column("usage_id", sa.Integer(), nullable=False),
        sa.Column("idempotency_key", sa.String(220), nullable=False),
        sa.Column("event_type", sa.String(40), nullable=False),
        sa.Column("units", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("remaining_after", sa.Integer(), nullable=True),
        sa.Column("metadata_json", sa.Text(), nullable=False, server_default="{}"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("idempotency_key", name="uq_subscription_trade_event_idempotency"),
    )
    op.create_index(
        "ix_subscription_trade_events_customer_created",
        "subscription_trade_events",
        ["customer_id", "created_at"],
    )

    op.create_table(
        "payment_intents",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("customer_id", sa.Integer(), nullable=False),
        sa.Column("subscription_id", sa.Integer(), nullable=True),
        sa.Column("plan_code", sa.String(40), nullable=False),
        sa.Column("billing_interval", sa.String(20), nullable=False),
        sa.Column("currency_usd", sa.String(10), nullable=False, server_default="USD"),
        sa.Column("amount_usd", sa.Numeric(38, 6), nullable=False),
        sa.Column("amount_usdt", sa.Numeric(38, 6), nullable=False),
        sa.Column("usdt_usd_rate", sa.Numeric(38, 8), nullable=False, server_default="1.00000000"),
        sa.Column("network", sa.String(20), nullable=False, server_default="TRON"),
        sa.Column("token_contract", sa.String(64), nullable=False),
        sa.Column("receiving_address", sa.String(64), nullable=False),
        sa.Column("payment_reference", sa.String(120), nullable=False),
        sa.Column("status", sa.String(30), nullable=False, server_default="PENDING"),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("payment_reference", name="uq_payment_intent_reference"),
    )
    op.create_index("ix_payment_intents_customer_status", "payment_intents", ["customer_id", "status"])

    op.create_table(
        "usdt_payment_verifications",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("payment_intent_id", sa.Integer(), nullable=False),
        sa.Column("customer_id", sa.Integer(), nullable=False),
        sa.Column("tx_hash", sa.String(128), nullable=False),
        sa.Column("network", sa.String(20), nullable=False, server_default="TRON"),
        sa.Column("token_contract", sa.String(64), nullable=False),
        sa.Column("sender_address", sa.String(64), nullable=False),
        sa.Column("recipient_address", sa.String(64), nullable=False),
        sa.Column("amount_usdt", sa.Numeric(38, 6), nullable=False),
        sa.Column("confirmations", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("verification_status", sa.String(30), nullable=False, server_default="PENDING"),
        sa.Column("verification_detail_json", sa.Text(), nullable=False, server_default="{}"),
        sa.Column("verified_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("tx_hash", name="uq_usdt_payment_tx_hash"),
    )
    op.create_index(
        "ix_usdt_payment_verifications_intent_status",
        "usdt_payment_verifications",
        ["payment_intent_id", "verification_status"],
    )

    op.create_table(
        "subscription_events",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("subscription_id", sa.Integer(), nullable=False),
        sa.Column("customer_id", sa.Integer(), nullable=False),
        sa.Column("event_type", sa.String(50), nullable=False),
        sa.Column("reference_id", sa.String(180), nullable=False, server_default=""),
        sa.Column("metadata_json", sa.Text(), nullable=False, server_default="{}"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("event_type", "reference_id", name="uq_subscription_event_reference"),
    )
    op.create_index(
        "ix_subscription_events_customer_created",
        "subscription_events",
        ["customer_id", "created_at"],
    )


def downgrade():
    op.drop_index("ix_subscription_events_customer_created", table_name="subscription_events")
    op.drop_table("subscription_events")
    op.drop_index("ix_usdt_payment_verifications_intent_status", table_name="usdt_payment_verifications")
    op.drop_table("usdt_payment_verifications")
    op.drop_index("ix_payment_intents_customer_status", table_name="payment_intents")
    op.drop_table("payment_intents")
    op.drop_index("ix_subscription_trade_events_customer_created", table_name="subscription_trade_events")
    op.drop_table("subscription_trade_events")
    op.drop_index("ix_subscription_trade_usage_customer_period", table_name="subscription_trade_usage")
    op.drop_table("subscription_trade_usage")
    op.drop_column("billing_plans", "monthly_trade_limit")
