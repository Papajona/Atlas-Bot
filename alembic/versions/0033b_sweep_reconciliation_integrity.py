"""Bind TRON sweeps to custody transfers and prevent transaction-id reuse."""
from alembic import op
import sqlalchemy as sa

revision = "0033_sweep_reconciliation_integrity"
down_revision = ("0032_platform_hardening", "0045_merge_application_heads")
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("tron_sweeps", sa.Column("custody_transfer_id", sa.Integer(), nullable=True))
    op.create_index("ix_tron_sweeps_custody_transfer_id", "tron_sweeps", ["custody_transfer_id"])
    op.create_index(
        "uq_tron_sweep_transaction_id_nonempty",
        "tron_sweeps",
        ["transaction_id"],
        unique=True,
        postgresql_where=sa.text("transaction_id <> ''"),
        sqlite_where=sa.text("transaction_id <> ''"),
    )


def downgrade():
    op.drop_index("uq_tron_sweep_transaction_id_nonempty", table_name="tron_sweeps")
    op.drop_index("ix_tron_sweeps_custody_transfer_id", table_name="tron_sweeps")
    op.drop_column("tron_sweeps", "custody_transfer_id")
