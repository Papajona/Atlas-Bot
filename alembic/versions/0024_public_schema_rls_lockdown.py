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
    # Prevent public API roles from creating objects in the exposed schema.
    op.execute("REVOKE CREATE ON SCHEMA public FROM PUBLIC, anon, authenticated")

    # Revoke existing direct access, including grants inherited from PUBLIC.
    op.execute("REVOKE ALL PRIVILEGES ON ALL TABLES IN SCHEMA public FROM PUBLIC, anon, authenticated")
    op.execute("REVOKE ALL PRIVILEGES ON ALL SEQUENCES IN SCHEMA public FROM PUBLIC, anon, authenticated")

    # Close default grants for objects created by the role running this migration.
    op.execute("ALTER DEFAULT PRIVILEGES IN SCHEMA public REVOKE ALL PRIVILEGES ON TABLES FROM PUBLIC, anon, authenticated")
    op.execute("ALTER DEFAULT PRIVILEGES IN SCHEMA public REVOKE ALL PRIVILEGES ON SEQUENCES FROM PUBLIC, anon, authenticated")

    # Fix the three mutable-search_path warnings found by the Supabase advisor.
    # Functions are SECURITY INVOKER in the audited database.
    op.execute("ALTER FUNCTION public.atlas_block_ledger_mutation() SET search_path = pg_catalog, public")
    op.execute("ALTER FUNCTION public.atlas_ledger_journal_deferred_check() SET search_path = pg_catalog, public")
    op.execute("ALTER FUNCTION public.atlas_validate_ledger_journal(integer) SET search_path = pg_catalog, public")

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
