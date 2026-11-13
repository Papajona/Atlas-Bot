"""Durable prospective trial chain and one-time holdout registry.

Revision ID: 0059_research_integrity_controls
Revises: 0058_customer_paper_rls_and_index_hardening
"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "0059_research_integrity_controls"
down_revision = "0058_customer_paper_rls_and_index_hardening"
branch_labels = None
depends_on = None


def _secure_service_tables(bind) -> None:
    if bind.dialect.name != "postgresql":
        return
    for table in ("research_trial_ledger_head", "research_holdout_registry"):
        op.execute(sa.text(f"ALTER TABLE public.{table} ENABLE ROW LEVEL SECURITY"))
        op.execute(sa.text(f"REVOKE ALL PRIVILEGES ON TABLE public.{table} FROM PUBLIC"))
    op.execute(sa.text("""
        DO $atlas_research_integrity_public_acl$
        BEGIN
            IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'anon') THEN
                EXECUTE 'REVOKE ALL PRIVILEGES ON TABLE public.research_trial_ledger_head FROM anon';
                EXECUTE 'REVOKE ALL PRIVILEGES ON TABLE public.research_holdout_registry FROM anon';
            END IF;
            IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'authenticated') THEN
                EXECUTE 'REVOKE ALL PRIVILEGES ON TABLE public.research_trial_ledger_head FROM authenticated';
                EXECUTE 'REVOKE ALL PRIVILEGES ON TABLE public.research_holdout_registry FROM authenticated';
            END IF;
        END
        $atlas_research_integrity_public_acl$;
    """))
    op.execute(sa.text("""
        DO $atlas_research_integrity_acl$
        DECLARE role_name text;
        BEGIN
            FOR role_name IN
                SELECT r.rolname
                FROM pg_roles r
                WHERE r.rolcanlogin AND r.rolbypassrls
                  AND NOT r.rolsuper AND NOT r.rolcreaterole AND NOT r.rolcreatedb
                  AND NOT r.rolreplication AND NOT r.rolinherit
                  AND NOT EXISTS (
                    SELECT 1 FROM pg_auth_members m WHERE m.member = r.oid
                  )
                  AND r.rolname NOT IN ('anon', 'authenticated')
            LOOP
                EXECUTE format(
                    'GRANT SELECT, UPDATE ON TABLE public.research_trial_ledger_head TO %I',
                    role_name
                );
                EXECUTE format(
                    'GRANT SELECT, INSERT ON TABLE public.research_holdout_registry TO %I',
                    role_name
                );
            END LOOP;
        END
        $atlas_research_integrity_acl$;
    """))
    op.execute(sa.text("""
        CREATE FUNCTION public.atlas_research_holdout_registry_immutable()
        RETURNS trigger
        LANGUAGE plpgsql
        AS $atlas_holdout_immutable$
        BEGIN
            RAISE EXCEPTION 'research_holdout_registry is write-once; UPDATE/DELETE is prohibited'
                USING ERRCODE = '55000';
        END
        $atlas_holdout_immutable$;
    """))
    op.execute(sa.text("""
        DROP TRIGGER IF EXISTS trg_research_holdout_registry_immutable
            ON public.research_holdout_registry;
        CREATE TRIGGER trg_research_holdout_registry_immutable
        BEFORE UPDATE OR DELETE ON public.research_holdout_registry
        FOR EACH ROW EXECUTE FUNCTION public.atlas_research_holdout_registry_immutable();
        DROP TRIGGER IF EXISTS trg_research_holdout_registry_no_truncate
            ON public.research_holdout_registry;
        CREATE TRIGGER trg_research_holdout_registry_no_truncate
        BEFORE TRUNCATE ON public.research_holdout_registry
        FOR EACH STATEMENT EXECUTE FUNCTION public.atlas_research_holdout_registry_immutable();
    """))


def upgrade() -> None:
    # Existing rows predate sequence/hash capture. The default explicitly labels
    # them legacy and unchained; no attempt is made to reconstruct missing history.
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    existing_tables = set(inspector.get_table_names(schema="public" if bind.dialect.name == "postgresql" else None))
    archive_columns = {
        column["name"] for column in inspector.get_columns(
            "research_trial_archive",
            schema="public" if bind.dialect.name == "postgresql" else None,
        )
    }
    new_columns = (
        sa.Column("ledger_sequence", sa.BigInteger(), nullable=True),
        sa.Column("ledger_previous_sha256", sa.String(64), nullable=True),
        sa.Column("ledger_entry_sha256", sa.String(64), nullable=True),
        sa.Column("ledger_hmac_sha256", sa.String(64), nullable=True),
        sa.Column("ledger_hmac_key_id", sa.String(100), nullable=True),
        sa.Column(
            "ledger_status",
            sa.String(40),
            nullable=False,
            server_default="LEGACY_UNCHAINED",
        ),
    )
    for column in new_columns:
        if column.name not in archive_columns:
            op.add_column(
                "research_trial_archive",
                column,
                schema="public" if bind.dialect.name == "postgresql" else None,
            )
    index_names = {
        item["name"] for item in inspector.get_indexes(
            "research_trial_archive",
            schema="public" if bind.dialect.name == "postgresql" else None,
        )
    }
    if "uq_research_trial_ledger_sequence" not in index_names:
        op.create_index(
            "uq_research_trial_ledger_sequence",
            "research_trial_archive",
            ["ledger_sequence"],
            unique=True,
            schema="public" if bind.dialect.name == "postgresql" else None,
        )

    if "research_trial_ledger_head" not in existing_tables:
        op.create_table(
            "research_trial_ledger_head",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("last_sequence", sa.BigInteger(), nullable=False, server_default="0"),
            sa.Column("last_entry_sha256", sa.String(64), nullable=False, server_default=""),
            sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
            sa.CheckConstraint("id = 1", name="ck_research_trial_ledger_head_singleton"),
            schema="public" if bind.dialect.name == "postgresql" else None,
        )
    if "research_holdout_registry" not in existing_tables:
        op.create_table(
            "research_holdout_registry",
            sa.Column("scope_sha256", sa.String(64), primary_key=True),
            sa.Column("holdout_version", sa.String(100), nullable=False),
            sa.Column("source_sample_sha256", sa.String(64), nullable=False),
            sa.Column("report_sha256", sa.String(64), nullable=False),
            sa.Column("holdout_plan_json", sa.Text(), nullable=False),
            sa.Column("consumed_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
            schema="public" if bind.dialect.name == "postgresql" else None,
        )
    head = bind.execute(sa.text(
        "SELECT count(*) FROM research_trial_ledger_head WHERE id = 1"
    )).scalar_one()
    if not head:
        bind.execute(sa.text(
            "INSERT INTO research_trial_ledger_head "
            "(id, last_sequence, last_entry_sha256) VALUES (1, 0, '')"
        ))
    _secure_service_tables(bind)


def downgrade() -> None:
    # This revision is forward-only because downgrading the schema would either
    # destroy one-time holdout consumption or make the trial chain unverifiable.
    # upgrade() is intentionally idempotent so CI downgrade/upgrade round-trips
    # can safely revisit this retained schema without recreating existing objects.
    pass

def downgrade() -> None:
    # Preserve append-only experiment records and holdout-consumption evidence.
    # Removing their integrity metadata would allow accidental reuse/rewrite.
    pass
