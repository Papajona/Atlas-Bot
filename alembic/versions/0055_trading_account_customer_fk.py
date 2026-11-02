"""Protect trading accounts with a customer-profile foreign key.

The composite child-table foreign keys introduced in 0052 protect account/customer
pairing, but do not protect the parent trading_accounts.customer_id relationship
from profile deletion or an orphaned legacy row.
"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa

revision = "0055_trading_account_customer_fk"
down_revision = "0054_audit_log_idempotency_key"
branch_labels = None
depends_on = None

CONSTRAINT = "fk_trading_accounts_customer_profile"


def upgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name != "postgresql":
        return

    op.execute(sa.text("SET LOCAL lock_timeout = '5s'"))
    orphan_count = bind.execute(sa.text("""
        SELECT count(*)
        FROM public.trading_accounts AS account
        LEFT JOIN public.customer_profiles AS profile
          ON profile.id = account.customer_id
        WHERE profile.id IS NULL
    """)).scalar_one()
    if int(orphan_count or 0):
        raise RuntimeError(
            "0055 refuses to create trading-account customer ownership FK: "
            f"{int(orphan_count)} trading_accounts rows reference missing customer_profiles"
        )

    exists = bind.execute(sa.text("""
        SELECT 1
        FROM pg_constraint
        WHERE conrelid = 'public.trading_accounts'::regclass
          AND conname = :constraint_name
    """), {"constraint_name": CONSTRAINT}).scalar_one_or_none()
    if exists:
        return

    op.execute(sa.text(f"""
        ALTER TABLE public.trading_accounts
        ADD CONSTRAINT {CONSTRAINT}
        FOREIGN KEY (customer_id)
        REFERENCES public.customer_profiles(id)
        ON DELETE RESTRICT
        NOT VALID
    """))
    op.execute(sa.text(
        f"ALTER TABLE public.trading_accounts VALIDATE CONSTRAINT {CONSTRAINT}"
    ))


def downgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name != "postgresql":
        return
    op.execute(sa.text("SET LOCAL lock_timeout = '5s'"))
    op.execute(sa.text(f"""
        ALTER TABLE public.trading_accounts
        DROP CONSTRAINT IF EXISTS {CONSTRAINT}
    """))
