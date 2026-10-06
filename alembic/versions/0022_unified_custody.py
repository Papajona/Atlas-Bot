"""Add unified custody observations and transfer state."""
from alembic import op
import sqlalchemy as sa

revision = "0022_unified_custody"
down_revision = "0021_tron_sweeps"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "custody_asset_observations",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("location", sa.String(length=180), nullable=False),
        sa.Column("provider", sa.String(length=50), nullable=False),
        sa.Column("customer_id", sa.Integer(), nullable=True),
        sa.Column("currency", sa.String(length=20), nullable=False, server_default="USDT"),
        sa.Column("balance", sa.Numeric(38, 18), nullable=False, server_default="0"),
        sa.Column("status", sa.String(length=30), nullable=False, server_default="ACTIVE"),
        sa.Column("source_reference", sa.String(length=180), nullable=False, server_default=""),
        sa.Column("observed_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=True),
        sa.UniqueConstraint("location", "currency", name="uq_custody_observation_location_currency"),
    )
    op.create_index("ix_custody_observation_status_observed", "custody_asset_observations", ["status", "observed_at"])
    op.create_index("ix_custody_asset_observation_customer", "custody_asset_observations", ["customer_id"])

    op.create_table(
        "custody_transfers",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("customer_id", sa.Integer(), nullable=True),
        sa.Column("currency", sa.String(length=20), nullable=False, server_default="USDT"),
        sa.Column("amount", sa.Numeric(38, 18), nullable=False, server_default="0"),
        sa.Column("source_location", sa.String(length=180), nullable=False),
        sa.Column("destination_location", sa.String(length=180), nullable=False),
        sa.Column("status", sa.String(length=40), nullable=False, server_default="REQUESTED"),
        sa.Column("idempotency_key", sa.String(length=220), nullable=False),
        sa.Column("provider_reference", sa.String(length=180), nullable=False, server_default=""),
        sa.Column("detail_json", sa.Text(), nullable=False, server_default="{}"),
        sa.Column("requested_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("submitted_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("confirmed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=True),
        sa.UniqueConstraint("idempotency_key", name="uq_custody_transfer_idempotency"),
    )
    op.create_index("ix_custody_transfer_status_updated", "custody_transfers", ["status", "updated_at"])
    op.create_index("ix_custody_transfer_customer", "custody_transfers", ["customer_id", "requested_at"])


def downgrade():
    op.drop_index("ix_custody_transfer_customer", table_name="custody_transfers")
    op.drop_index("ix_custody_transfer_status_updated", table_name="custody_transfers")
    op.drop_table("custody_transfers")
    op.drop_index("ix_custody_asset_observation_customer", table_name="custody_asset_observations")
    op.drop_index("ix_custody_observation_status_observed", table_name="custody_asset_observations")
    op.drop_table("custody_asset_observations")
