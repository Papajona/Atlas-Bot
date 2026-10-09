"""Lock down the exposed public schema for API roles.

The application accesses PostgreSQL through the trusted backend. The public
Supabase API roles must not have direct table/sequence privileges. RLS is
enabled on every public base/partitioned table without adding permissive
policies; this intentionally denies row access to anon/authenticated until a
table-specific policy is reviewed and added. The backend's trusted database
role must be configured separately and must not be anon/authenticated.
"""
from alembic import op

revision = "0024_public_schema_rls_lockdown"
down_revision = "0023_security_billing_hardening"
branch_labels = None
depends_on = None


def upgrade():
    # Revoke existing direct access, including grants inherited from PUBLIC.
    op.execute("REVOKE ALL PRIVILEGES ON ALL TABLES IN SCHEMA public FROM PUBLIC, anon, authenticated")
    op.execute("REVOKE ALL PRIVILEGES ON ALL SEQUENCES IN SCHEMA public FROM PUBLIC, anon, authenticated")

    # Close current and future default grants for the role running this migration.
    op.execute("ALTER DEFAULT PRIVILEGES IN SCHEMA public REVOKE ALL PRIVILEGES ON TABLES FROM PUBLIC, anon, authenticated")
    op.execute("ALTER DEFAULT PRIVILEGES IN SCHEMA public REVOKE ALL PRIVILEGES ON SEQUENCES FROM PUBLIC, anon, authenticated")

    # Enable RLS on every ordinary or partitioned table in public. No generic
    # policies are created: customer ownership and finance roles need explicit,
    # application-specific policy design rather than a blanket auth.uid() rule.
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
    # Deliberately do not re-open API grants or disable RLS on financial tables.
    # A rollback must not silently restore the insecure pre-migration posture.
    pass
