"""Append-only prospective research trial archive.

Revision ID: 0051_research_trial_archive
Revises: 0050_paper_execution_lifecycle

This table records future experiments. It does not itself certify historical
trial completeness or make promotion eligible.
"""
from alembic import op
import sqlalchemy as sa

revision = "0051_research_trial_archive"
down_revision = "0050_paper_execution_lifecycle"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "research_trial_archive",
        sa.Column("trial_key", sa.String(64), primary_key=True),
        sa.Column("trial_label", sa.String(180), nullable=False),
        sa.Column("asset_class", sa.String(30), nullable=False, server_default=""),
        sa.Column("venue", sa.String(80), nullable=False, server_default=""),
        sa.Column("market_type", sa.String(30), nullable=False, server_default=""),
        sa.Column("symbol", sa.String(100), nullable=False, server_default=""),
        sa.Column("timeframe", sa.String(20), nullable=False, server_default=""),
        sa.Column("config_sha256", sa.String(64), nullable=False, server_default=""),
        sa.Column("code_sha256", sa.String(64), nullable=False, server_default=""),
        sa.Column("dataset_sha256", sa.String(64), nullable=False, server_default=""),
        sa.Column("oos_sample_sha256", sa.String(64), nullable=False, server_default=""),
        sa.Column("protocol_version", sa.String(100), nullable=False, server_default=""),
        sa.Column("universe_evidence_sha256", sa.String(64), nullable=False, server_default=""),
        sa.Column("cost_evidence_id", sa.String(180), nullable=False, server_default=""),
        sa.Column("annualized_sharpe", sa.Float(), nullable=True),
        sa.Column("oos_total_return", sa.Float(), nullable=True),
        sa.Column("max_drawdown", sa.Float(), nullable=True),
        sa.Column("trades", sa.Integer(), nullable=True),
        sa.Column("results_sha256", sa.String(64), nullable=False, server_default=""),
        sa.Column("archive_status", sa.String(40), nullable=False, server_default="UNVERIFIED_MISSING_CONTEXT"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
    )
    op.create_index(
        "ix_research_trial_context_sample", "research_trial_archive",
        ["asset_class", "venue", "market_type", "symbol", "timeframe", "oos_sample_sha256"],
    )
    op.create_index("ix_research_trial_created_at", "research_trial_archive", ["created_at"])
    op.execute("ALTER TABLE public.research_trial_archive ENABLE ROW LEVEL SECURITY")
    op.execute("REVOKE ALL PRIVILEGES ON TABLE public.research_trial_archive FROM PUBLIC")
    op.execute(
        """
        DO $atlas_research_trial_acl$
        DECLARE role_name text;
        BEGIN
            FOREACH role_name IN ARRAY ARRAY['anon', 'authenticated'] LOOP
                IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = role_name) THEN
                    EXECUTE format('REVOKE ALL PRIVILEGES ON TABLE public.research_trial_archive FROM %I', role_name);
                END IF;
            END LOOP;
            -- Application runtime roles are backend-only roles: LOGIN, NOINHERIT,
            -- BYPASSRLS, no elevated database/role capabilities, and no memberships.
            -- Grant only these roles table access; public API roles remain revoked.
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
                EXECUTE format('GRANT SELECT, INSERT ON TABLE public.research_trial_archive TO %I', role_name);
            END LOOP;
        END
        $atlas_research_trial_acl$;
        """
    )
    op.execute(
        """
        CREATE FUNCTION public.atlas_research_trial_archive_immutable()
        RETURNS trigger
        LANGUAGE plpgsql
        AS $atlas_trial_immutable$
        BEGIN
            RAISE EXCEPTION 'research_trial_archive is append-only; UPDATE/DELETE is prohibited'
                USING ERRCODE = '55000';
        END
        $atlas_trial_immutable$;
        """
    )
    op.execute(
        """
        CREATE TRIGGER trg_research_trial_archive_immutable
        BEFORE UPDATE OR DELETE ON public.research_trial_archive
        FOR EACH ROW EXECUTE FUNCTION public.atlas_research_trial_archive_immutable();
        """
    )


def downgrade():
    # A no-op downgrade would move Alembic's version marker backwards while leaving
    # the archive table, trigger, and function in place; a subsequent upgrade would
    # then fail at CREATE TABLE. Preserve both the audit rows and schema/version
    # consistency by refusing implicit rollback. Retention/removal needs a separate,
    # explicitly reviewed operation.
    raise RuntimeError(
        "0051_research_trial_archive is intentionally irreversible; "
        "perform a reviewed research-archive retention operation before rollback"
    )
