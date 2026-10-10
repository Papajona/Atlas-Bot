"""Lock down the exposed public schema for API roles.

Public Supabase API roles must not have direct table/sequence privileges. RLS
is enabled without generic permissive policies; direct API access requires
separately reviewed table-specific policies.

This migration deliberately does not add blanket customer policies. The Atlas
application is expected to use a trusted server-side PostgreSQL role; that
connection-role assumption must be verified in staging before rollout.
"""
from alembic import op

revision = "0049_public_schema_rls_lockdown"
down_revision = "0048_merge_subscription_and_application_heads"
branch_labels = None
depends_on = None


def upgrade():
    # These default privileges apply to objects created by the role running this
    # migration. Owners/roles that create public objects independently need their
    # own default-privilege review.
    op.execute(
        """
        DO $atlas_grants$
        DECLARE
            role_name text;
        BEGIN
            EXECUTE 'REVOKE CREATE ON SCHEMA public FROM PUBLIC';
            EXECUTE 'REVOKE ALL PRIVILEGES ON ALL TABLES IN SCHEMA public FROM PUBLIC';
            EXECUTE 'REVOKE ALL PRIVILEGES ON ALL SEQUENCES IN SCHEMA public FROM PUBLIC';
            EXECUTE 'ALTER DEFAULT PRIVILEGES IN SCHEMA public REVOKE ALL PRIVILEGES ON TABLES FROM PUBLIC';
            EXECUTE 'ALTER DEFAULT PRIVILEGES IN SCHEMA public REVOKE ALL PRIVILEGES ON SEQUENCES FROM PUBLIC';

            FOREACH role_name IN ARRAY ARRAY['anon', 'authenticated'] LOOP
                IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = role_name) THEN
                    EXECUTE format('REVOKE CREATE ON SCHEMA public FROM %I', role_name);
                    EXECUTE format('REVOKE ALL PRIVILEGES ON ALL TABLES IN SCHEMA public FROM %I', role_name);
                    EXECUTE format('REVOKE ALL PRIVILEGES ON ALL SEQUENCES IN SCHEMA public FROM %I', role_name);
                    EXECUTE format('ALTER DEFAULT PRIVILEGES IN SCHEMA public REVOKE ALL PRIVILEGES ON TABLES FROM %I', role_name);
                    EXECUTE format('ALTER DEFAULT PRIVILEGES IN SCHEMA public REVOKE ALL PRIVILEGES ON SEQUENCES FROM %I', role_name);
                    EXECUTE format('ALTER DEFAULT PRIVILEGES IN SCHEMA public REVOKE EXECUTE ON FUNCTIONS FROM %I', role_name);
                END IF;
            END LOOP;

            EXECUTE 'ALTER DEFAULT PRIVILEGES IN SCHEMA public REVOKE EXECUTE ON FUNCTIONS FROM PUBLIC';
        END
        $atlas_grants$;
        """
    )

    # Pin search_path for the ledger functions flagged by the security advisor.
    op.execute(
        """
        DO $atlas_function_paths$
        BEGIN
            IF to_regprocedure('public.atlas_block_ledger_mutation()') IS NOT NULL THEN
                EXECUTE 'ALTER FUNCTION public.atlas_block_ledger_mutation() SET search_path = pg_catalog, public';
            END IF;
            IF to_regprocedure('public.atlas_ledger_journal_deferred_check()') IS NOT NULL THEN
                EXECUTE 'ALTER FUNCTION public.atlas_ledger_journal_deferred_check() SET search_path = pg_catalog, public';
            END IF;
            IF to_regprocedure('public.atlas_validate_ledger_journal(integer)') IS NOT NULL THEN
                EXECUTE 'ALTER FUNCTION public.atlas_validate_ledger_journal(integer) SET search_path = pg_catalog, public';
            END IF;
        END
        $atlas_function_paths$;
        """
    )

    # These internal routines are not client RPC endpoints. Other public functions
    # require a separate authorization inventory rather than blanket revocation.
    op.execute(
        """
        DO $atlas_function_grants$
        DECLARE
            role_name text;
            function_signature text;
        BEGIN
            FOREACH function_signature IN ARRAY ARRAY[
                'public.atlas_block_ledger_mutation()',
                'public.atlas_ledger_journal_deferred_check()',
                'public.atlas_validate_ledger_journal(integer)'
            ] LOOP
                IF to_regprocedure(function_signature) IS NOT NULL THEN
                    EXECUTE format('REVOKE EXECUTE ON FUNCTION %s FROM PUBLIC', function_signature);
                    FOREACH role_name IN ARRAY ARRAY['anon', 'authenticated'] LOOP
                        IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = role_name) THEN
                            EXECUTE format('REVOKE EXECUTE ON FUNCTION %s FROM %I', function_signature, role_name);
                        END IF;
                    END LOOP;
                END IF;
            END LOOP;
        END
        $atlas_function_grants$;
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
    # Security posture is intentionally not weakened on rollback. Reversing this
    # migration requires a separately reviewed, explicit access-restoration plan.
    pass
