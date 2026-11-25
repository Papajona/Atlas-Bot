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
            migration_role_oid oid;
            migration_role_superuser boolean;
            function_signature text;
            grantee_name text;
            role_created_by_revision boolean := false;
            journals_policy_exists boolean;
            lines_policy_exists boolean;
            journals_policy_created_by_revision boolean := false;
            lines_policy_created_by_revision boolean := false;
            role_comment text;
            role_state jsonb;
        BEGIN
            IF migration_role IN ('atlas_api_runtime', 'atlas_audit_reader') THEN
                RAISE EXCEPTION
                    '0065 must run as an administrative migration identity, not an application or audit role';
            END IF;

            IF to_regprocedure('public.atlas_ledger_journal_deferred_check()') IS NULL
               OR to_regprocedure('public.atlas_validate_ledger_journal(integer)') IS NULL THEN
                RAISE EXCEPTION
                    'ledger journal trigger and validation functions must exist before trigger-owner hardening';
            END IF;

            SELECT oid INTO role_oid
            FROM pg_roles
            WHERE rolname = role_name;

            IF role_oid IS NULL THEN
                EXECUTE 'CREATE ROLE atlas_ledger_trigger_owner NOLOGIN NOINHERIT NOBYPASSRLS NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION';
                role_created_by_revision := true;
            ELSE
                role_comment := shobj_description(role_oid, 'pg_authid');
                IF role_comment LIKE 'atlas:0065:rollback-state:v1:%'
                   OR role_comment LIKE 'atlas:0065:rollback-state:v2:%'
                   OR role_comment LIKE 'atlas:0065:rollback-state:v3:%' THEN
                    RAISE EXCEPTION
                        'atlas_ledger_trigger_owner already carries 0065 rollback metadata; inspect migration state before retrying';
                END IF;
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

            SELECT EXISTS (
                SELECT 1 FROM pg_policy
                WHERE polrelid = to_regclass('public.ledger_journals')
                  AND polname = 'atlas_ledger_trigger_owner_select'
            ) INTO journals_policy_exists;
            SELECT EXISTS (
                SELECT 1 FROM pg_policy
                WHERE polrelid = to_regclass('public.ledger_journal_lines')
                  AND polname = 'atlas_ledger_trigger_owner_select'
            ) INTO lines_policy_exists;

            journals_policy_created_by_revision := NOT journals_policy_exists;
            lines_policy_created_by_revision := NOT lines_policy_exists;

            IF journals_policy_created_by_revision THEN
                EXECUTE 'CREATE POLICY atlas_ledger_trigger_owner_select ON public.ledger_journals FOR SELECT TO atlas_ledger_trigger_owner USING (true)';
            END IF;
            IF lines_policy_created_by_revision THEN
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

            -- Store rollback provenance on the role itself. PostgreSQL DDL and
            -- this comment are transactional, so a failed upgrade cannot leave
            -- partial ownership markers. Reused roles retain their prior comment
            -- and grants when this revision is downgraded.
            SELECT oid INTO migration_role_oid
            FROM pg_roles
            WHERE rolname = migration_role;
            IF migration_role_oid IS NULL THEN
                RAISE EXCEPTION
                    '0065 migration identity is missing from pg_roles; cannot record rollback grant provenance';
            END IF;

            -- Record the direct ACL entries from the migration grantor, not
            -- effective privileges. has_*_privilege() includes PUBLIC and
            -- would make downgrade retain direct grants added by this revision.
            SELECT oid INTO migration_role_oid
            FROM pg_roles
            WHERE rolname = migration_role;
            IF migration_role_oid IS NULL THEN
                RAISE EXCEPTION
                    '0065 migration identity is missing from pg_roles; cannot record rollback grant provenance';
            END IF;

            -- Snapshot the trigger role's direct ACL entries before 0065
            -- changes them. A privilege inherited from PUBLIC is not a direct
            -- grant; a grant from any existing owner/grantor is preserved.
            role_state := jsonb_build_object(
                'marker', 'atlas:0065:rollback-state:v3',
                'role_created_by_revision', role_created_by_revision,
                'journals_policy_created_by_revision', journals_policy_created_by_revision,
                'lines_policy_created_by_revision', lines_policy_created_by_revision,
                'migration_identity', migration_role,
                'pre_public_schema_usage_acl', coalesce((
                    SELECT jsonb_agg(jsonb_build_object(
                        'grantor', acl.grantor,
                        'privilege_type', acl.privilege_type,
                        'is_grantable', acl.is_grantable
                    ))
                    FROM pg_namespace n
                    CROSS JOIN LATERAL aclexplode(
                        coalesce(n.nspacl, acldefault('n', n.nspowner))
                    ) acl
                    WHERE n.nspname = 'public'
                      AND acl.grantee = role_oid
                      AND acl.privilege_type = 'USAGE'
                ), '[]'::jsonb),
                'pre_ledger_journals_select_acl', coalesce((
                    SELECT jsonb_agg(jsonb_build_object(
                        'grantor', acl.grantor,
                        'privilege_type', acl.privilege_type,
                        'is_grantable', acl.is_grantable
                    ))
                    FROM pg_class c
                    CROSS JOIN LATERAL aclexplode(
                        coalesce(c.relacl, acldefault('r', c.relowner))
                    ) acl
                    WHERE c.oid = to_regclass('public.ledger_journals')
                      AND acl.grantee = role_oid
                      AND acl.privilege_type = 'SELECT'
                ), '[]'::jsonb),
                'pre_ledger_journal_lines_select_acl', coalesce((
                    SELECT jsonb_agg(jsonb_build_object(
                        'grantor', acl.grantor,
                        'privilege_type', acl.privilege_type,
                        'is_grantable', acl.is_grantable
                    ))
                    FROM pg_class c
                    CROSS JOIN LATERAL aclexplode(
                        coalesce(c.relacl, acldefault('r', c.relowner))
                    ) acl
                    WHERE c.oid = to_regclass('public.ledger_journal_lines')
                      AND acl.grantee = role_oid
                      AND acl.privilege_type = 'SELECT'
                ), '[]'::jsonb),
                'pre_validate_function_execute_acl', coalesce((
                    SELECT jsonb_agg(jsonb_build_object(
                        'grantor', acl.grantor,
                        'privilege_type', acl.privilege_type,
                        'is_grantable', acl.is_grantable
                    ))
                    FROM pg_proc p
                    CROSS JOIN LATERAL aclexplode(
                        coalesce(p.proacl, acldefault('f', p.proowner))
                    ) acl
                    WHERE p.oid = to_regprocedure(
                        'public.atlas_validate_ledger_journal(integer)'
                    )
                      AND acl.grantee = role_oid
                      AND acl.privilege_type = 'EXECUTE'
                ), '[]'::jsonb),
                'prior_role_comment', role_comment
            );

            IF EXISTS (
                SELECT 1
                FROM jsonb_array_elements(
                    role_state->'pre_validate_function_execute_acl'
                ) AS grant_item
                JOIN pg_roles grantor
                  ON grantor.oid = (grant_item->>'grantor')::oid
                WHERE grantor.oid <> (
                    SELECT proowner
                    FROM pg_proc
                    WHERE oid = to_regprocedure(
                        'public.atlas_validate_ledger_journal(integer)'
                    )
                )
                  AND (
                      grantor.rolcanlogin
                      OR grantor.rolsuper
                      OR grantor.rolcreatedb
                      OR grantor.rolcreaterole
                      OR grantor.rolreplication
                      OR EXISTS (
                          SELECT 1
                          FROM pg_auth_members membership
                          WHERE membership.roleid = grantor.oid
                             OR membership.member = grantor.oid
                      )
                  )
            ) THEN
                RAISE EXCEPTION
                    '0065 cannot preserve a pre-existing trigger-owner validation grant whose grantor is login-capable, privileged, or has role memberships; remove or isolate that grant before retrying';
            END IF;

            role_state := role_state || jsonb_build_object(
                'preserved_validate_function_grantor_oids', coalesce((
                    SELECT jsonb_agg(DISTINCT (grant_item->>'grantor')::oid)
                    FROM jsonb_array_elements(
                        role_state->'pre_validate_function_execute_acl'
                    ) AS grant_item
                    JOIN pg_roles grantor
                      ON grantor.oid = (grant_item->>'grantor')::oid
                    WHERE grantor.oid <> (
                        SELECT proowner
                        FROM pg_proc
                        WHERE oid = to_regprocedure(
                            'public.atlas_validate_ledger_journal(integer)'
                        )
                    )
                      AND NOT grantor.rolcanlogin
                      AND NOT grantor.rolsuper
                      AND NOT grantor.rolcreatedb
                      AND NOT grantor.rolcreaterole
                      AND NOT grantor.rolreplication
                      AND NOT EXISTS (
                          SELECT 1
                          FROM pg_auth_members membership
                          WHERE membership.roleid = grantor.oid
                             OR membership.member = grantor.oid
                      )
                ), '[]'::jsonb)
            );

            EXECUTE format(
                'COMMENT ON ROLE %I IS %L',
                role_name,
                'atlas:0065:rollback-state:v3:' || role_state::text
            );

            -- Do not add another grantor's ACL entry if a direct grant already
            -- exists. PUBLIC-derived privileges do not count as direct grants.
            IF jsonb_array_length(role_state->'pre_public_schema_usage_acl') = 0 THEN
                EXECUTE 'GRANT USAGE ON SCHEMA public TO atlas_ledger_trigger_owner';
            END IF;
            IF jsonb_array_length(role_state->'pre_ledger_journals_select_acl') = 0 THEN
                EXECUTE 'GRANT SELECT ON TABLE public.ledger_journals TO atlas_ledger_trigger_owner';
            END IF;
            IF jsonb_array_length(role_state->'pre_ledger_journal_lines_select_acl') = 0 THEN
                EXECUTE 'GRANT SELECT ON TABLE public.ledger_journal_lines TO atlas_ledger_trigger_owner';
            END IF;

            SELECT proowner INTO trigger_function_owner
            FROM pg_proc
            WHERE oid = to_regprocedure('public.atlas_ledger_journal_deferred_check()');

            SELECT oid, rolsuper
              INTO migration_role_oid, migration_role_superuser
            FROM pg_roles
            WHERE rolname = migration_role;

            IF trigger_function_owner <> role_oid THEN
                IF migration_role = role_name THEN
                    RAISE EXCEPTION
                        'migration must use a separate administrative identity to assign the ledger trigger owner';
                END IF;

                IF NOT migration_role_superuser AND EXISTS (
                    SELECT 1
                    FROM pg_proc p
                    WHERE p.oid IN (
                        to_regprocedure('public.atlas_block_ledger_mutation()'),
                        to_regprocedure('public.atlas_ledger_journal_deferred_check()'),
                        to_regprocedure('public.atlas_validate_ledger_journal(integer)'),
                        to_regprocedure('public.atlas_research_trial_archive_immutable()'),
                        to_regprocedure('public.atlas_research_holdout_registry_immutable()')
                    )
                      AND p.proowner <> migration_role_oid
                ) THEN
                    RAISE EXCEPTION
                        'migration identity must own every internal ledger function before ACL hardening';
                END IF;

                -- Enumerate every explicit EXECUTE grantee, not only the known
                -- application roles. The current function owner has grant
                -- authority before ownership is transferred to the isolated role.
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

                    FOR grantee_name IN
                        SELECT DISTINCT grantee.rolname
                        FROM pg_proc p
                        CROSS JOIN LATERAL aclexplode(
                            coalesce(p.proacl, acldefault('f', p.proowner))
                        ) acl
                        JOIN pg_roles grantee ON grantee.oid = acl.grantee
                        WHERE p.oid = to_regprocedure(function_signature)
                          AND acl.privilege_type = 'EXECUTE'
                          AND acl.grantee <> p.proowner
                          AND grantee.rolname <> role_name
                          AND NOT (
                              p.oid = to_regprocedure(
                                  'public.atlas_validate_ledger_journal(integer)'
                              )
                              AND grantee.oid IN (
                                  SELECT grantor_oid::oid
                                  FROM jsonb_array_elements_text(
                                      coalesce(
                                          role_state->'preserved_validate_function_grantor_oids',
                                          '[]'::jsonb
                                      )
                                  ) AS preserved(grantor_oid)
                              )
                          )
                    LOOP
                        EXECUTE format(
                            'REVOKE EXECUTE ON FUNCTION %s FROM %I CASCADE',
                            function_signature,
                            grantee_name
                        );
                    END LOOP;
                END LOOP;
                IF jsonb_array_length(role_state->'pre_validate_function_execute_acl') = 0 THEN
                    EXECUTE 'GRANT EXECUTE ON FUNCTION public.atlas_validate_ledger_journal(integer) TO atlas_ledger_trigger_owner';
                END IF;

                -- Compare pre/post direct ACLs and retain only entries introduced
                -- by this revision. Store actual grantor OIDs, which can differ
                -- from current_user when authority is inherited from an owner.
                role_state := role_state || jsonb_build_object(
                    'introduced_public_schema_usage_grantors', coalesce((
                        SELECT jsonb_agg(DISTINCT acl.grantor)
                        FROM pg_namespace n
                        CROSS JOIN LATERAL aclexplode(
                            coalesce(n.nspacl, acldefault('n', n.nspowner))
                        ) acl
                        WHERE n.nspname = 'public'
                          AND acl.grantee = role_oid
                          AND acl.privilege_type = 'USAGE'
                          AND NOT (
                              role_state->'pre_public_schema_usage_acl'
                              @> jsonb_build_array(jsonb_build_object(
                                  'grantor', acl.grantor,
                                  'privilege_type', acl.privilege_type,
                                  'is_grantable', acl.is_grantable
                              ))
                          )
                    ), '[]'::jsonb),
                    'introduced_ledger_journals_select_grantors', coalesce((
                        SELECT jsonb_agg(DISTINCT acl.grantor)
                        FROM pg_class c
                        CROSS JOIN LATERAL aclexplode(
                            coalesce(c.relacl, acldefault('r', c.relowner))
                        ) acl
                        WHERE c.oid = to_regclass('public.ledger_journals')
                          AND acl.grantee = role_oid
                          AND acl.privilege_type = 'SELECT'
                          AND NOT (
                              role_state->'pre_ledger_journals_select_acl'
                              @> jsonb_build_array(jsonb_build_object(
                                  'grantor', acl.grantor,
                                  'privilege_type', acl.privilege_type,
                                  'is_grantable', acl.is_grantable
                              ))
                          )
                    ), '[]'::jsonb),
                    'introduced_ledger_journal_lines_select_grantors', coalesce((
                        SELECT jsonb_agg(DISTINCT acl.grantor)
                        FROM pg_class c
                        CROSS JOIN LATERAL aclexplode(
                            coalesce(c.relacl, acldefault('r', c.relowner))
                        ) acl
                        WHERE c.oid = to_regclass('public.ledger_journal_lines')
                          AND acl.grantee = role_oid
                          AND acl.privilege_type = 'SELECT'
                          AND NOT (
                              role_state->'pre_ledger_journal_lines_select_acl'
                              @> jsonb_build_array(jsonb_build_object(
                                  'grantor', acl.grantor,
                                  'privilege_type', acl.privilege_type,
                                  'is_grantable', acl.is_grantable
                              ))
                          )
                    ), '[]'::jsonb),
                    'introduced_validate_function_execute_grantors', coalesce((
                        SELECT jsonb_agg(DISTINCT acl.grantor)
                        FROM pg_proc p
                        CROSS JOIN LATERAL aclexplode(
                            coalesce(p.proacl, acldefault('f', p.proowner))
                        ) acl
                        WHERE p.oid = to_regprocedure(
                            'public.atlas_validate_ledger_journal(integer)'
                        )
                          AND acl.grantee = role_oid
                          AND acl.privilege_type = 'EXECUTE'
                          AND NOT (
                              role_state->'pre_validate_function_execute_acl'
                              @> jsonb_build_array(jsonb_build_object(
                                  'grantor', acl.grantor,
                                  'privilege_type', acl.privilege_type,
                                  'is_grantable', acl.is_grantable
                              ))
                          )
                    ), '[]'::jsonb)
                );
                EXECUTE format(
                    'COMMENT ON ROLE %I IS %L',
                    role_name,
                    'atlas:0065:rollback-state:v3:' || role_state::text
                );

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
                    WHERE p.oid = to_regprocedure(function_signature)
                      AND acl.privilege_type = 'EXECUTE'
                      AND acl.grantee <> p.proowner
                      AND NOT (
                          p.oid = to_regprocedure('public.atlas_validate_ledger_journal(integer)')
                          AND (
                              acl.grantee = role_oid
                              OR acl.grantee IN (
                                  SELECT grantor_oid::oid
                                  FROM jsonb_array_elements_text(
                                      coalesce(
                                          role_state->'preserved_validate_function_grantor_oids',
                                          '[]'::jsonb
                                      )
                                  ) AS preserved(grantor_oid)
                              )
                          )
                      )
                ) THEN
                    RAISE EXCEPTION
                        'internal function % retains an unapproved explicit EXECUTE grant after trigger-owner isolation',
                        function_signature;
                END IF;
            END LOOP;

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
    """Restore the original migration owner and remove the isolated trigger role.

    PUBLIC and application EXECUTE grants deliberately remain revoked. This
    makes Alembic downgrade/upgrade round-trips safe without reopening the
    internal trigger functions to runtime identities.
    """
    bind = op.get_bind()
    if bind.dialect.name != "postgresql":
        return

    op.execute(sa.text("""
        DO $atlas_ledger_trigger_owner_downgrade$
        DECLARE
            role_name constant text := 'atlas_ledger_trigger_owner';
            migration_role text := current_user;
            role_oid oid;
            function_owner oid;
            role_comment text;
            role_state jsonb;
            migration_role_oid oid;
            migration_role_superuser boolean;
            grantor_oid oid;
            grantor_name text;
        BEGIN
            SELECT oid INTO role_oid
            FROM pg_roles
            WHERE rolname = role_name;

            IF role_oid IS NULL THEN
                RETURN;
            END IF;

            role_comment := shobj_description(role_oid, 'pg_authid');
            IF role_comment LIKE 'atlas:0065:rollback-state:v3:%' THEN
                role_state := substring(
                    role_comment FROM length('atlas:0065:rollback-state:v3:') + 1
                )::jsonb;
            ELSE
                RAISE EXCEPTION
                    'cannot safely downgrade 0065: rollback provenance is missing or predates direct-ACL snapshots; preserving the trigger role and policies';
            END IF;

            IF role_state->>'marker' IS DISTINCT FROM 'atlas:0065:rollback-state:v3'
               OR nullif(role_state->>'migration_identity', '') IS NULL THEN
                RAISE EXCEPTION
                    'cannot safely downgrade 0065: rollback provenance marker or migration identity is malformed';
            END IF;

            -- Require the same migration identity used at upgrade. The identity
            -- must retain the ability to assume each recorded ACL grantor role.
            IF migration_role IS DISTINCT FROM role_state->>'migration_identity' THEN
                RAISE EXCEPTION
                    'cannot safely downgrade 0065: run Alembic as the original migration identity % (current identity: %)',
                    role_state->>'migration_identity',
                    migration_role;
            END IF;

            SELECT oid, rolsuper
              INTO migration_role_oid, migration_role_superuser
            FROM pg_roles
            WHERE rolname = migration_role;

            IF migration_role_oid IS NULL THEN
                RAISE EXCEPTION
                    'cannot safely downgrade 0065: recorded migration identity no longer exists';
            END IF;

            -- Validate every grantor recorded by the ACL diff before any
            -- ownership/policy changes. Missing grantors or missing membership
            -- authority must fail closed and leave the migration untouched.
            FOR grantor_oid IN
                SELECT DISTINCT grantor_text::oid
                FROM (
                    SELECT jsonb_array_elements_text(
                        coalesce(role_state->'introduced_public_schema_usage_grantors', '[]'::jsonb)
                    ) AS grantor_text
                    UNION ALL
                    SELECT jsonb_array_elements_text(
                        coalesce(role_state->'introduced_ledger_journals_select_grantors', '[]'::jsonb)
                    )
                    UNION ALL
                    SELECT jsonb_array_elements_text(
                        coalesce(role_state->'introduced_ledger_journal_lines_select_grantors', '[]'::jsonb)
                    )
                    UNION ALL
                    SELECT jsonb_array_elements_text(
                        coalesce(role_state->'introduced_validate_function_execute_grantors', '[]'::jsonb)
                    )
                ) grantors
            LOOP
                SELECT rolname INTO grantor_name
                FROM pg_roles
                WHERE oid = grantor_oid;

                IF grantor_name IS NULL THEN
                    RAISE EXCEPTION
                        'cannot safely downgrade 0065: an ACL grantor recorded during upgrade no longer exists';
                END IF;

                IF NOT migration_role_superuser
                   AND grantor_oid <> migration_role_oid
                   AND NOT pg_has_role(migration_role_oid, grantor_oid, 'MEMBER') THEN
                    RAISE EXCEPTION
                        'cannot safely downgrade 0065: original migration identity % is no longer a member of ACL grantor %',
                        migration_role,
                        grantor_name;
                END IF;
            END LOOP;

            SELECT proowner INTO function_owner
            FROM pg_proc
            WHERE oid = to_regprocedure('public.atlas_ledger_journal_deferred_check()');

            IF function_owner IS DISTINCT FROM role_oid THEN
                RAISE EXCEPTION
                    'cannot downgrade 0065: deferred ledger trigger is not owned by atlas_ledger_trigger_owner';
            END IF;

            IF NOT has_schema_privilege(migration_role, 'public', 'CREATE') THEN
                RAISE EXCEPTION
                    'cannot downgrade 0065: migration identity needs CREATE on public to reclaim trigger ownership';
            END IF;

            -- Become a temporary member of the current owner so PostgreSQL
            -- permits OWNER TO migration_role. Remove that membership as soon
            -- as ownership is restored.
            EXECUTE format('GRANT %I TO %I', role_name, migration_role);
            EXECUTE format(
                'ALTER FUNCTION public.atlas_ledger_journal_deferred_check() OWNER TO %I',
                migration_role
            );
            EXECUTE format('REVOKE %I FROM %I', role_name, migration_role);

            -- Roll back only state created by this revision. Policies that
            -- predated 0065 and a pre-provisioned trigger-owner role are retained.
            IF coalesce((role_state->>'journals_policy_created_by_revision')::boolean, false) THEN
                EXECUTE 'DROP POLICY atlas_ledger_trigger_owner_select ON public.ledger_journals';
            END IF;
            IF coalesce((role_state->>'lines_policy_created_by_revision')::boolean, false) THEN
                EXECUTE 'DROP POLICY atlas_ledger_trigger_owner_select ON public.ledger_journal_lines';
            END IF;

            -- Revoke only entries proven by the pre/post snapshots to have
            -- been introduced by 0065. Every pre-existing grantor entry remains.
            FOR grantor_oid IN
                SELECT DISTINCT grantor_text::oid
                FROM jsonb_array_elements_text(
                    coalesce(role_state->'introduced_validate_function_execute_grantors', '[]'::jsonb)
                ) AS grantors(grantor_text)
            LOOP
                SELECT rolname INTO grantor_name FROM pg_roles WHERE oid = grantor_oid;
                IF grantor_oid <> migration_role_oid THEN
                    EXECUTE format('SET ROLE %I', grantor_name);
                END IF;
                EXECUTE format(
                    'REVOKE EXECUTE ON FUNCTION public.atlas_validate_ledger_journal(integer) FROM %I',
                    role_name
                );
                IF grantor_oid <> migration_role_oid THEN
                    EXECUTE format('SET ROLE %I', migration_role);
                END IF;
            END LOOP;

            FOR grantor_oid IN
                SELECT DISTINCT grantor_text::oid
                FROM jsonb_array_elements_text(
                    coalesce(role_state->'introduced_ledger_journals_select_grantors', '[]'::jsonb)
                ) AS grantors(grantor_text)
            LOOP
                SELECT rolname INTO grantor_name FROM pg_roles WHERE oid = grantor_oid;
                IF grantor_oid <> migration_role_oid THEN
                    EXECUTE format('SET ROLE %I', grantor_name);
                END IF;
                EXECUTE format(
                    'REVOKE SELECT ON TABLE public.ledger_journals FROM %I',
                    role_name
                );
                IF grantor_oid <> migration_role_oid THEN
                    EXECUTE format('SET ROLE %I', migration_role);
                END IF;
            END LOOP;

            FOR grantor_oid IN
                SELECT DISTINCT grantor_text::oid
                FROM jsonb_array_elements_text(
                    coalesce(role_state->'introduced_ledger_journal_lines_select_grantors', '[]'::jsonb)
                ) AS grantors(grantor_text)
            LOOP
                SELECT rolname INTO grantor_name FROM pg_roles WHERE oid = grantor_oid;
                IF grantor_oid <> migration_role_oid THEN
                    EXECUTE format('SET ROLE %I', grantor_name);
                END IF;
                EXECUTE format(
                    'REVOKE SELECT ON TABLE public.ledger_journal_lines FROM %I',
                    role_name
                );
                IF grantor_oid <> migration_role_oid THEN
                    EXECUTE format('SET ROLE %I', migration_role);
                END IF;
            END LOOP;

            FOR grantor_oid IN
                SELECT DISTINCT grantor_text::oid
                FROM jsonb_array_elements_text(
                    coalesce(role_state->'introduced_public_schema_usage_grantors', '[]'::jsonb)
                ) AS grantors(grantor_text)
            LOOP
                SELECT rolname INTO grantor_name FROM pg_roles WHERE oid = grantor_oid;
                IF grantor_oid <> migration_role_oid THEN
                    EXECUTE format('SET ROLE %I', grantor_name);
                END IF;
                EXECUTE format(
                    'REVOKE USAGE ON SCHEMA public FROM %I',
                    role_name
                );
                IF grantor_oid <> migration_role_oid THEN
                    EXECUTE format('SET ROLE %I', migration_role);
                END IF;
            END LOOP;

            IF coalesce((role_state->>'role_created_by_revision')::boolean, false)
               AND NOT EXISTS (
                    SELECT 1
                    FROM pg_shdepend
                    WHERE refclassid = 'pg_authid'::regclass
                      AND refobjid = role_oid
               ) THEN
                -- Drop only a role created here that has no remaining shared
                -- dependencies. If an operator added unrelated grants or
                -- memberships after upgrade, preserve the role and complete
                -- the downgrade without DROP OWNED or destructive revocation.
                EXECUTE 'DROP ROLE atlas_ledger_trigger_owner';
            ELSE
                -- Restore the original comment for reused roles and for
                -- revision-created roles that acquired independent dependencies.
                EXECUTE format(
                    'COMMENT ON ROLE %I IS %L',
                    role_name,
                    role_state->>'prior_role_comment'
                );
            END IF;
        END
        $atlas_ledger_trigger_owner_downgrade$;
    """))
