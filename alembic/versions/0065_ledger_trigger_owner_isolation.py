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
                EXECUTE 'CREATE ROLE atlas_ledger_trigger_owner NOLOGIN NOINHERIT BYPASSRLS NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION';
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
                  AND rolbypassrls
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

            EXECUTE 'GRANT USAGE ON SCHEMA public TO atlas_ledger_trigger_owner';
            EXECUTE 'GRANT SELECT ON TABLE public.ledger_journals, public.ledger_journal_lines TO atlas_ledger_trigger_owner';
            EXECUTE 'GRANT EXECUTE ON FUNCTION public.atlas_validate_ledger_journal(integer) TO atlas_ledger_trigger_owner';

            SELECT proowner INTO trigger_function_owner
            FROM pg_proc
            WHERE oid = to_regprocedure('public.atlas_ledger_journal_deferred_check()');

            IF trigger_function_owner <> role_oid THEN
                IF migration_role = role_name THEN
                    RAISE EXCEPTION
                        'migration must use a separate administrative identity to assign the ledger trigger owner';
                END IF;

                -- Configure the wrapper while the migration actor still owns
                -- it. The helper's explicit pg_temp-last path prevents temporary
                -- relation shadowing during its read-only ledger validation.
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
                EXECUTE format('REVOKE %I FROM %I', role_name, migration_role);
                EXECUTE 'REVOKE CREATE ON SCHEMA public FROM atlas_ledger_trigger_owner';
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

            -- Explicitly revoke the built-in PUBLIC EXECUTE grant and any
            -- direct grants inherited from managed/default object creators.
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

                EXECUTE format(
                    'REVOKE EXECUTE ON FUNCTION %s FROM PUBLIC',
                    function_signature
                );

                FOREACH grantee_name IN ARRAY ARRAY[
                    'anon', 'authenticated', 'service_role',
                    'atlas_paper_reader', 'atlas_customer_paper',
                    'atlas_api_runtime', 'atlas_audit_reader'
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

            -- Only the internal trigger owner may call the validator helper.
            EXECUTE 'GRANT EXECUTE ON FUNCTION public.atlas_validate_ledger_journal(integer) TO atlas_ledger_trigger_owner';

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
    # This is a forward-only privilege hardening revision. Reversing it would
    # restore direct/public EXECUTE privileges and weaken the ledger boundary.
    pass
