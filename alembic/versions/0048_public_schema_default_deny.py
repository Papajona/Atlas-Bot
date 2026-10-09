"""Default-deny public schema access for Supabase client roles.

The FastAPI service uses its direct PostgreSQL connection; no repository code
uses Supabase PostgREST for application-table CRUD. Client roles therefore
receive no direct public-schema table, sequence, or function privileges.
RLS is enabled as defense in depth, without FORCE ROW LEVEL SECURITY, so the
trusted backend database role can continue its existing server-side work.
"""
from alembic import op
import sqlalchemy as sa

revision = "0048_public_schema_default_deny"
down_revision = "0047_strategy_outcome_lifecycle"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name != "postgresql":
        return

    # Enable RLS for every existing public table, including tables introduced
    # by migrations that predate this hardening revision.
    op.execute(sa.text("""
        DO $atlas$
        DECLARE
            item record;
        BEGIN
            FOR item IN
                SELECT n.nspname, c.relname
                FROM pg_class AS c
                JOIN pg_namespace AS n ON n.oid = c.relnamespace
                WHERE n.nspname = 'public'
                  AND c.relkind IN ('r', 'p')
                  AND NOT c.relrowsecurity
            LOOP
                EXECUTE format(
                    'ALTER TABLE %I.%I ENABLE ROW LEVEL SECURITY',
                    item.nspname, item.relname
                );
            END LOOP;
        END
        $atlas$;
    """))

    # Revoke client-role access to existing objects. Role existence checks
    # keep the migration usable in isolated PostgreSQL CI databases that do
    # not install Supabase's anon/authenticated/service_role roles.
    op.execute(sa.text("""
        DO $atlas$
        BEGIN
            EXECUTE
                'REVOKE ALL PRIVILEGES ON ALL TABLES IN SCHEMA public FROM PUBLIC';

            EXECUTE
                'REVOKE ALL PRIVILEGES ON ALL SEQUENCES IN SCHEMA public FROM PUBLIC';

            EXECUTE
                'REVOKE ALL PRIVILEGES ON ALL FUNCTIONS IN SCHEMA public FROM PUBLIC';

            IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'anon')
               AND EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'authenticated') THEN
                EXECUTE
                    'REVOKE ALL PRIVILEGES ON ALL TABLES IN SCHEMA public FROM anon, authenticated';
                EXECUTE
                    'REVOKE ALL PRIVILEGES ON ALL SEQUENCES IN SCHEMA public FROM anon, authenticated';
                EXECUTE
                    'REVOKE ALL PRIVILEGES ON ALL FUNCTIONS IN SCHEMA public FROM anon, authenticated';
            END IF;

            IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'postgres') THEN
                EXECUTE
                    'GRANT EXECUTE ON ALL FUNCTIONS IN SCHEMA public TO postgres';
            END IF;

            IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'service_role') THEN
                EXECUTE
                    'GRANT EXECUTE ON ALL FUNCTIONS IN SCHEMA public TO service_role';
            END IF;
        END
        $atlas$;
    """))

    # Harden defaults for objects created by the role applying this migration
    # (the repository's deployment workflow uses the postgres DB role).
    op.execute(sa.text("""
        DO $atlas$
        BEGIN
            EXECUTE
                'ALTER DEFAULT PRIVILEGES IN SCHEMA public REVOKE EXECUTE ON FUNCTIONS FROM PUBLIC';

            IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'anon')
               AND EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'authenticated') THEN
                EXECUTE
                    'ALTER DEFAULT PRIVILEGES IN SCHEMA public REVOKE ALL ON TABLES FROM anon, authenticated';
                EXECUTE
                    'ALTER DEFAULT PRIVILEGES IN SCHEMA public REVOKE ALL ON SEQUENCES FROM anon, authenticated';
                EXECUTE
                    'ALTER DEFAULT PRIVILEGES IN SCHEMA public REVOKE EXECUTE ON FUNCTIONS FROM anon, authenticated';
            END IF;
        END
        $atlas$;
    """))

    # The ledger trigger functions use unqualified public-table references.
    # Pin their search_path to avoid caller-controlled object resolution.
    op.execute(sa.text("""
        DO $atlas$
        BEGIN
            IF to_regprocedure('public.atlas_validate_ledger_journal(integer)') IS NOT NULL THEN
                EXECUTE 'ALTER FUNCTION public.atlas_validate_ledger_journal(integer) SET search_path = pg_catalog, public';
            END IF;
            IF to_regprocedure('public.atlas_ledger_journal_deferred_check()') IS NOT NULL THEN
                EXECUTE 'ALTER FUNCTION public.atlas_ledger_journal_deferred_check() SET search_path = pg_catalog, public';
            END IF;
            IF to_regprocedure('public.atlas_block_ledger_mutation()') IS NOT NULL THEN
                EXECUTE 'ALTER FUNCTION public.atlas_block_ledger_mutation() SET search_path = pg_catalog, public';
            END IF;
        END
        $atlas$;
    """))


def downgrade() -> None:
    # Deliberately do not restore broad client grants or remove RLS on rollback.
    # Reopening public financial/trading tables during a rollback would be unsafe.
    pass
