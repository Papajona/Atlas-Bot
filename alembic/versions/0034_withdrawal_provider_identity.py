"""Prevent one provider payout identifier from settling multiple withdrawals."""
from alembic import op
import sqlalchemy as sa

revision = "0034_withdrawal_provider_identity"
down_revision = "0033_sweep_reconciliation_integrity"
branch_labels = None
depends_on = None

def upgrade():
    op.create_index(
        "uq_withdrawal_provider_id_nonempty",
        "withdrawals",
        ["provider", "provider_id"],
        unique=True,
        postgresql_where=sa.text("provider_id <> ''"),
        sqlite_where=sa.text("provider_id <> ''"),
    )

def downgrade():
    op.drop_index("uq_withdrawal_provider_id_nonempty", table_name="withdrawals")
