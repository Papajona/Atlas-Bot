"""Pin research immutability trigger function resolution.

Revision ID: 0066_pin_research_trigger_path
Revises: 0065_ledger_trigger_owner_isolation

The trigger bodies only raise an exception and need no application-schema
resolution. Keep their runtime search path limited to pg_catalog.
"""
from alembic import op


revision = "0066_pin_research_trigger_path"
down_revision = "0065_ledger_trigger_owner_isolation"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name != "postgresql":
        return

    op.execute("""
        DO $atlas_research_trigger_path$
        DECLARE
            function_signature text;
            role_name text;
        BEGIN
            FOREACH function_signature IN ARRAY ARRAY[
                'public.atlas_research_trial_archive_immutable()',
                'public.atlas_research_holdout_registry_immutable()'
            ] LOOP
                IF to_regprocedure(function_signature) IS NULL THEN
                    RAISE EXCEPTION
                        'required research immutability trigger function is missing: %',
                        function_signature;
                END IF;

                EXECUTE format(
                    'ALTER FUNCTION %s SET search_path = pg_catalog',
                    function_signature
                );
                -- 0065 already enforces internal-function ACLs. Repeat the
                -- PUBLIC revoke here so this migration independently preserves
                -- the no-public-execution invariant for both trigger functions.
                EXECUTE format(
                    'REVOKE EXECUTE ON FUNCTION %s FROM PUBLIC',
                    function_signature
                );

                -- PUBLIC revocation removes the inherited default grant; also
                -- remove any historical direct grants to Supabase API roles.
                FOREACH role_name IN ARRAY ARRAY['anon', 'authenticated'] LOOP
                    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = role_name) THEN
                        EXECUTE format(
                            'REVOKE EXECUTE ON FUNCTION %s FROM %I',
                            function_signature,
                            role_name
                        );
                        IF has_function_privilege(
                            role_name,
                            to_regprocedure(function_signature),
                            'EXECUTE'
                        ) THEN
                            RAISE EXCEPTION
                                'Supabase API role % retains EXECUTE on internal research trigger function: %',
                                role_name, function_signature;
                        END IF;
                    END IF;
                END LOOP;

                IF NOT EXISTS (
                    SELECT 1
                    FROM pg_proc p
                    WHERE p.oid = to_regprocedure(function_signature)
                      AND p.proconfig @> ARRAY['search_path=pg_catalog']
                ) THEN
                    RAISE EXCEPTION
                        'research trigger function search_path was not pinned: %',
                        function_signature;
                END IF;

                IF EXISTS (
                    SELECT 1
                    FROM pg_proc p
                    CROSS JOIN LATERAL aclexplode(
                        coalesce(p.proacl, acldefault('f', p.proowner))
                    ) acl
                    WHERE p.oid = to_regprocedure(function_signature)
                      AND acl.grantee = 0
                      AND acl.privilege_type = 'EXECUTE'
                ) THEN
                    RAISE EXCEPTION
                        'PUBLIC retains EXECUTE on internal research trigger function: %',
                        function_signature;
                END IF;
            END LOOP;
        END
        $atlas_research_trigger_path$;
    """)


def downgrade() -> None:
    # Security hardening is intentionally retained on rollback.
    pass
