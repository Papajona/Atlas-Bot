"""Merge the customer-pilot and unified-custody Alembic branches without rewriting revisions."""

revision = "0045_merge_application_heads"
down_revision = ("0044_customer_pilot_risk_controls", "0022_unified_custody")
branch_labels = None
depends_on = None


def upgrade() -> None:
    pass


def downgrade() -> None:
    pass
