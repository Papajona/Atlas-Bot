"""Isolate internal ledger trigger execution from API and audit roles.

Revision ID: 0065_ledger_trigger_owner_isolation
Revises: 0064_strategy_outcome_recent_index
"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "0065_ledger_trigger_owner_isolation"
down_revision = "0064_strategy_outcome_recent_index"
branch_labels = None
depends_on = None


def _secure_ledger_journal_trigger_owner(bind) -> None:
    """Run deferred ledger validation under a non-login, read-only function owner.

    The production and audit identities deliberately receive no EXECUTE grants
    on public functions. The deferred trigger still needs to call the validation
    helper, so its SECURITY DEFINER context is isolated in a NOLOGIN role with
    SELECT-only ledger access and no role memberships.
    """
    if bind.dialect.name != "postgresql":
        return

    op.execute(sa.text("""
        DO $atlas_ledger_trigger_owner$
        DECLARE
            role_name constant text := 'atlas_ledger_trigger_owner';
            migration_role text := current_user;
            role_oid oid;
            trigger_function_owner oid;
            function_signature text;
            grantee_name text;
        BEGIN
            IF to_regprocedure('public.atlas_ledger_journal_deferred_check()') IS NULL
               OR to_regprocedure('public.atlas_validate_ledger_journal(integer)') IS NULL THEN
                RAISE EXCEPTION
                    'ledger journal trigger and validation functions must exist before trigger-owner hardening';
            END IF;

            IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = role_name) THEN
                EXECUTE 'CREATE ROLE atlas_ledger_trigger_owner NOLOGIN NOINHERIT NOBYPASSRLS NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION';
            END IF;

            SELECT oid INTO role_oid
            FROM pg_roles
            WHERE rolname = role_name;

            IF NOT EXISTS (
                SELECT 1
                FROM pg_roles
                WHERE oid = role_oid
                  AND NOT rolcanlogin
                  AND NOT rolinherit
                  AND NOT rolbypassrls
                  AND NOT rolsuper
                  AND NOT rolcreatedb
                  AND NOT rolcreaterole
                  AND NOT rolreplication
            ) THEN
                RAISE EXCEPTION
                    'atlas_ledger_trigger_owner has unsafe role attributes; inspect it rather than widening or reusing it';
            END IF;

            IF EXISTS (
                SELECT 1
                FROM pg_auth_members
                WHERE roleid = role_oid OR member = role_oid
            ) THEN
                RAISE EXCEPTION
                    'atlas_ledger_trigger_owner must not have or be granted any role memberships';
            END IF;

            IF EXISTS (
                SELECT 1
                FROM pg_class
                WHERE relowner = role_oid
            ) THEN
                RAISE EXCEPTION
                    'atlas_ledger_trigger_owner must not own tables, sequences, or other relations';
            END IF;

            -- This role must work on managed PostgreSQL services that do not
            -- permit application migration identities to create BYPASSRLS roles.
            -- Its read access is granted narrowly by SELECT policies on the two
            -- ledger tables; no other RLS boundary is bypassed.
            IF NOT EXISTS (
                SELECT 1 FROM pg_class
                WHERE oid = to_regclass('public.ledger_journals')
                  AND relrowsecurity
            ) OR NOT EXISTS (
                SELECT 1 FROM pg_class
                WHERE oid = to_regclass('public.ledger_journal_lines')
                  AND relrowsecurity
            ) THEN
                RAISE EXCEPTION
                    'ledger journal tables must have row-level security enabled before trigger-owner provisioning';
            END IF;

            IF NOT EXISTS (
                SELECT 1 FROM pg_policy
                WHERE polrelid = to_regclass('public.ledger_journals')
                  AND polname = 'atlas_ledger_trigger_owner_select'
            ) THEN
                EXECUTE 'CREATE POLICY atlas_ledger_trigger_owner_select ON public.ledger_journals FOR SELECT TO atlas_ledger_trigger_owner USING (true)';
            END IF;
            IF NOT EXISTS (
                SELECT 1 FROM pg_policy
                WHERE polrelid = to_regclass('public.ledger_journal_lines')
                  AND polname = 'atlas_ledger_trigger_owner_select'
            ) THEN
                EXECUTE 'CREATE POLICY atlas_ledger_trigger_owner_select ON public.ledger_journal_lines FOR SELECT TO atlas_ledger_trigger_owner USING (true)';
            END IF;

            IF NOT EXISTS (
                SELECT 1 FROM pg_policy
                WHERE polrelid = to_regclass('public.ledger_journals')
                  AND polname = 'atlas_ledger_trigger_owner_select'
                  AND polcmd = 'r'
                  AND role_oid = ANY(polroles)
                  AND pg_get_expr(polqual, polrelid) = 'true'
            ) OR NOT EXISTS (
                SELECT 1 FROM pg_policy
                WHERE polrelid = to_regclass('public.ledger_journal_lines')
                  AND polname = 'atlas_ledger_trigger_owner_select'
                  AND polcmd = 'r'
                  AND role_oid = ANY(polroles)
                  AND pg_get_expr(polqual, polrelid) = 'true'
            ) THEN
                RAISE EXCEPTION
                    'ledger trigger owner SELECT policies are missing or have an unexpected definition';
            END IF;

            EXECUTE 'GRANT USAGE ON SCHEMA public TO atlas_ledger_trigger_owner';
            EXECUTE 'GRANT SELECT ON TABLE public.ledger_journals, public.ledger_journal_lines TO atlas_ledger_trigger_owner';

            SELECT proowner INTO trigger_function_owner
            FROM pg_proc
            WHERE oid = to_regprocedure('public.atlas_ledger_journal_deferred_check()');

            IF trigger_function_owner <> role_oid THEN
                IF migration_role = role_name THEN
                    RAISE EXCEPTION
                        'migration must use a separate administrative identity to assign the ledger trigger owner';
                END IF;

                -- Revoke all external EXECUTE grants while the migration actor
                -- still owns the original functions. After OWNER TO is changed,
                -- a non-superuser migration actor cannot revoke the new owner's
                -- grants merely because it once held role membership.
                FOREACH function_signature IN ARRAY ARRAY[
                    'public.atlas_block_ledger_mutation()',
                    'public.atlas_ledger_journal_deferred_check()',
                    'public.atlas_validate_ledger_journal(integer)',
                    'public.atlas_research_trial_archive_immutable()',
                    'public.atlas_research_holdout_registry_immutable()'
                ] LOOP
                    EXECUTE format(
                        'REVOKE EXECUTE ON FUNCTION %s FROM PUBLIC',
                        function_signature
                    );
                    FOREACH grantee_name IN ARRAY ARRAY[
                        'anon', 'authenticated', 'service_role',
                        'atlas_paper_reader', 'atlas_customer_paper',
                        'atlas_api_runtime', 'atlas_audit_reader',
                        'atlas_ledger_trigger_owner'
                    ] LOOP
                        IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = grantee_name) THEN
                            EXECUTE format(
                                'REVOKE EXECUTE ON FUNCTION %s FROM %I',
                                function_signature,
                                grantee_name
                            );
                        END IF;
                    END LOOP;
                END LOOP;
                EXECUTE 'GRANT EXECUTE ON FUNCTION public.atlas_validate_ledger_journal(integer) TO atlas_ledger_trigger_owner';

                -- Configure the wrapper while the migration actor still owns it.
                EXECUTE 'ALTER FUNCTION public.atlas_ledger_journal_deferred_check() SECURITY DEFINER';
                EXECUTE 'ALTER FUNCTION public.atlas_ledger_journal_deferred_check() SET search_path = pg_catalog, public, pg_temp';
                EXECUTE 'ALTER FUNCTION public.atlas_validate_ledger_journal(integer) SET search_path = pg_catalog, public, pg_temp';

                -- ALTER FUNCTION OWNER requires CREATE on its schema and the
                -- migration actor to be able to SET ROLE to the new owner.
                EXECUTE 'GRANT CREATE ON SCHEMA public TO atlas_ledger_trigger_owner';
                EXECUTE format(
                    'GRANT %I TO %I',
                    role_name,
                    migration_role
                );
                EXECUTE 'ALTER FUNCTION public.atlas_ledger_journal_deferred_check() OWNER TO atlas_ledger_trigger_owner';
                EXECUTE 'REVOKE CREATE ON SCHEMA public FROM atlas_ledger_trigger_owner';
                EXECUTE format('REVOKE %I FROM %I', role_name, migration_role);
            END IF;

            -- A downgrade/upgrade round-trip re-enters this function with the
            -- trigger owner already assigned. Do not mutate the function as a
            -- caller that is no longer its owner; verify the pinned attributes.
            IF NOT EXISTS (
                SELECT 1
                FROM pg_proc p
                WHERE p.oid = to_regprocedure('public.atlas_ledger_journal_deferred_check()')
                  AND p.prosecdef
                  AND p.proconfig @> ARRAY['search_path=pg_catalog, public, pg_temp']
            ) OR NOT EXISTS (
                SELECT 1
                FROM pg_proc p
                WHERE p.oid = to_regprocedure('public.atlas_validate_ledger_journal(integer)')
                  AND p.proconfig @> ARRAY['search_path=pg_catalog, public, pg_temp']
            ) THEN
                RAISE EXCEPTION
                    'ledger validation functions must use their fixed pg_catalog/public/pg_temp search_path';
            END IF;

            -- Re-upgrades must prove the previous ACL hardening is still in
            -- place; they must not attempt REVOKE as a caller that no longer
            -- owns the deferred trigger function.
            FOREACH function_signature IN ARRAY ARRAY[
                'public.atlas_block_ledger_mutation()',
                'public.atlas_ledger_journal_deferred_check()',
                'public.atlas_validate_ledger_journal(integer)',
                'public.atlas_research_trial_archive_immutable()',
                'public.atlas_research_holdout_registry_immutable()'
            ] LOOP
                IF to_regprocedure(function_signature) IS NULL THEN
                    RAISE EXCEPTION 'required internal trigger function is missing: %', function_signature;
                END IF;

                IF EXISTS (
                    SELECT 1
                    FROM pg_proc p
                    CROSS JOIN LATERAL aclexplode(
                        coalesce(p.proacl, acldefault('f', p.proowner))
                    ) acl
                    LEFT JOIN pg_roles grantee ON grantee.oid = acl.grantee
                    WHERE p.oid = to_regprocedure(function_signature)
                      AND acl.privilege_type = 'EXECUTE'
                      AND (
                          acl.grantee = 0
                          OR grantee.rolname = ANY(ARRAY[
                              'anon', 'authenticated', 'service_role',
                              'atlas_paper_reader', 'atlas_customer_paper',
                              'atlas_api_runtime', 'atlas_audit_reader'
                          ])
                      )
                ) THEN
                    RAISE EXCEPTION
                        'internal function % retains an external EXECUTE grant after trigger-owner isolation',
                        function_signature;
                END IF;
            END LOOP;

            IF EXISTS (
                SELECT 1
                FROM pg_proc p
                CROSS JOIN LATERAL aclexplode(
                    coalesce(p.proacl, acldefault('f', p.proowner))
                ) acl
                WHERE p.oid IN (
                    to_regprocedure('public.atlas_block_ledger_mutation()'),
                    to_regprocedure('public.atlas_ledger_journal_deferred_check()'),
                    to_regprocedure('public.atlas_validate_ledger_journal(integer)'),
                    to_regprocedure('public.atlas_research_trial_archive_immutable()'),
                    to_regprocedure('public.atlas_research_holdout_registry_immutable()')
                )
                  AND acl.grantee = role_oid
                  AND p.oid NOT IN (
                      to_regprocedure('public.atlas_ledger_journal_deferred_check()'),
                      to_regprocedure('public.atlas_validate_ledger_journal(integer)')
                  )
            ) THEN
                RAISE EXCEPTION
                    'atlas_ledger_trigger_owner has an unexpected direct EXECUTE grant';
            END IF;

            IF has_schema_privilege(role_name, 'public', 'CREATE')
               OR NOT has_schema_privilege(role_name, 'public', 'USAGE')
               OR NOT has_table_privilege(role_name, 'public.ledger_journals', 'SELECT')
               OR NOT has_table_privilege(role_name, 'public.ledger_journal_lines', 'SELECT')
               OR has_table_privilege(role_name, 'public.ledger_journals', 'INSERT')
               OR has_table_privilege(role_name, 'public.ledger_journals', 'UPDATE')
               OR has_table_privilege(role_name, 'public.ledger_journals', 'DELETE')
               OR has_table_privilege(role_name, 'public.ledger_journal_lines', 'INSERT')
               OR has_table_privilege(role_name, 'public.ledger_journal_lines', 'UPDATE')
               OR has_table_privilege(role_name, 'public.ledger_journal_lines', 'DELETE')
               OR NOT has_function_privilege(
                    role_name,
                    'public.atlas_validate_ledger_journal(integer)',
                    'EXECUTE'
               ) THEN
                RAISE EXCEPTION
                    'atlas_ledger_trigger_owner does not match its required schema/table/function allowlist';
            END IF;

            IF EXISTS (
                SELECT 1
                FROM pg_auth_members
                WHERE roleid = role_oid OR member = role_oid
            ) THEN
                RAISE EXCEPTION
                    'atlas_ledger_trigger_owner unexpectedly has a role membership after ownership transfer';
            END IF;

            IF (
                SELECT count(*)
                FROM pg_proc p
                WHERE p.proowner = role_oid
            ) <> 1
               OR NOT EXISTS (
                    SELECT 1
                    FROM pg_proc p
                    WHERE p.oid = to_regprocedure('public.atlas_ledger_journal_deferred_check()')
                      AND p.proowner = role_oid
                      AND p.prosecdef
                      AND p.proconfig @> ARRAY['search_path=pg_catalog, public, pg_temp']
               ) THEN
                RAISE EXCEPTION
                    'atlas_ledger_trigger_owner must own only the fixed-search-path deferred trigger function';
            END IF;
        END
        $atlas_ledger_trigger_owner$;
    """))


def upgrade() -> None:
    _secure_ledger_journal_trigger_owner(op.get_bind())


def downgrade() -> None:
    # Reverting ownership here would let an older migration drop the deferred
    # trigger function only if it could safely reclaim the former owner role.
    # The migration actor is intentionally not left as a member of that role.
    # Fail at this revision with a precise message rather than silently leave
    # the owner changed and allow a later legacy DROP FUNCTION to fail mid-rollback.
    raise RuntimeError(
        "downgrade past 0065_ledger_trigger_owner_isolation is blocked: "
        "restore ledger trigger ownership through a separately reviewed migration"
    )
