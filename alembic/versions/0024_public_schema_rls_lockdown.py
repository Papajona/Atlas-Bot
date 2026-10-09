"""Lock down the exposed public schema for API roles.

The application uses PostgreSQL through its trusted backend. Public Supabase API
roles must not have direct table/sequence privileges. RLS is enabled without
generic permissive policies; each table requires an explicitly reviewed policy
before direct anon/authenticated API access is granted.
"""
from alembic import op

revision = "0024_public_schema_rls_lockdown"
down_revision = "0023_security_billing_hardening"
branch_labels = None
depends_on = None


def upgrade():
    # Use guarded role checks so the migration also runs on plain PostgreSQL CI,
    # where Supabase's anon/authenticated roles may not exist.
    op.execute(
        """
        DO $atlas_grants$
        DECLARE
            role_name text;
        BEGIN
            REVOKE CREATE ON SCHEMA public FROM PUBLIC;
            REVOKE ALL PRIVILEGES ON ALL TABLES IN SCHEMA public FROM PUBLIC;
            REVOKE ALL PRIVILEGES ON ALL SEQUENCES IN SCHEMA public FROM PUBLIC;
            ALTER DEFAULT PRIVILEGES IN SCHEMA public REVOKE ALL PRIVILEGES ON TABLES FROM PUBLIC;
            ALTER DEFAULT PRIVILEGES IN SCHEMA public REVOKE ALL PRIVILEGES ON SEQUENCES FROM PUBLIC;

            FOREACH role_name IN ARRAY ARRAY['anon', 'authenticated'] LOOP
                IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = role_name) THEN
                    EXECUTE format('REVOKE CREATE ON SCHEMA public FROM %I', role_name);
                    EXECUTE format('REVOKE ALL PRIVILEGES ON ALL TABLES IN SCHEMA public FROM %I', role_name);
                    EXECUTE format('REVOKE ALL PRIVILEGES ON ALL SEQUENCES IN SCHEMA public FROM %I', role_name);
                    EXECUTE format('ALTER DEFAULT PRIVILEGES IN SCHEMA public REVOKE ALL PRIVILEGES ON TABLES FROM %I', role_name);
                    EXECUTE format('ALTER DEFAULT PRIVILEGES IN SCHEMA public REVOKE ALL PRIVILEGES ON SEQUENCES FROM %I', role_name);
                END IF;
            END LOOP;
        END
        $atlas_grants$;
        """
    )

    # Pin search_path for the three ledger functions reported by the Supabase
    # advisor. Guard each alteration for migration portability.
    op.execute(
        """
        DO $atlas_function_paths$
        BEGIN
            IF to_regprocedure('public.atlas_block_ledger_mutation()') IS NOT NULL THEN
                ALTER FUNCTION public.atlas_block_ledger_mutation() SET search_path = pg_catalog, public;
            END IF;
            IF to_regprocedure('public.atlas_ledger_journal_deferred_check()') IS NOT NULL THEN
                ALTER FUNCTION public.atlas_ledger_journal_deferred_check() SET search_path = pg_catalog, public;
            END IF;
            IF to_regprocedure('public.atlas_validate_ledger_journal(integer)') IS NOT NULL THEN
                ALTER FUNCTION public.atlas_validate_ledger_journal(integer) SET search_path = pg_catalog, public;
            END IF;
        END
        $atlas_function_paths$;
        """
    )

    # No generic policies: customer ownership and finance permissions require
    # table-specific authorization rather than a blanket auth.uid() rule.
    op.execute(
        """
        DO $atlas_rls$
        DECLARE
            item record;
        BEGIN
            FOR item IN
                SELECT n.nspname AS schema_name, c.relname AS table_name
                FROM pg_class AS c
                JOIN pg_namespace AS n ON n.oid = c.relnamespace
                WHERE n.nspname = 'public'
                  AND c.relkind IN ('r', 'p')
            LOOP
                EXECUTE format(
                    'ALTER TABLE %I.%I ENABLE ROW LEVEL SECURITY',
                    item.schema_name,
                    item.table_name
                );
            END LOOP;
        END
        $atlas_rls$;
        """
    )


def downgrade():
    # Deliberately keep the security posture on downgrade; never reopen public
    # table access or disable RLS automatically on financial tables.
    pass
