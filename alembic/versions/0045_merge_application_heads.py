"""Merge the two existing Alembic branches without rewriting history."""

from alembic import op

revision = "0045_merge_application_heads"
down_revision = ("0044_customer_kyc_username", "0023_security_billing_hardening")
branch_labels = None
depends_on = None


def upgrade() -> None:
    pass


def downgrade() -> None:
    pass
