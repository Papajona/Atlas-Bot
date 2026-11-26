"""Restrict service_role on Atlas-owned public objects.

Managed extension objects are not owned by the Alembic migration identity and
must not be included in broad schema-wide REVOKE statements. Future defaults
are checked separately so an unmodifiable managed-owner grant fails closed.
"""
from alembic import op
import sqlalchemy as sa


revision = "0067_service_role_customer_profiles_acl"
down_revision = "0066_pin_research_trigger_path"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name != "postgresql":
        return

    op.execute(sa.text("""
        DO $atlas_service_role_public_acl$
        DECLARE
            service_role_oid oid;
            migration_role_oid oid;
            public_schema_oid oid;
            profile_table_oid oid;
            profile_sequence_oid oid;
            profile_sequence text;
            object_record record;
            function_record record;
            default_acl_owner text;
            default_acl_object_type text;
            has_maintain boolean := false;
        BEGIN
            SELECT oid INTO service_role_oid
            FROM pg_roles
            WHERE rolname = 'service_role';

            IF service_role_oid IS NULL THEN
                RETURN;
            END IF;

            SELECT oid INTO migration_role_oid
            FROM pg_roles
            WHERE rolname = current_user;

            SELECT oid INTO public_schema_oid
            FROM pg_namespace
            WHERE nspname = 'public';

            IF migration_role_oid IS NULL OR public_schema_oid IS NULL THEN
                RAISE EXCEPTION
                    '0067 could not resolve its migration role or public schema';
            END IF;

            profile_table_oid := to_regclass('public.customer_profiles');
            IF profile_table_oid IS NULL THEN
                RAISE EXCEPTION
                    '0067 requires public.customer_profiles before restricting service_role';
            END IF;

            profile_sequence := pg_get_serial_sequence(
                'public.customer_profiles', 'id'
            );
            profile_sequence_oid := to_regclass('public.customer_profiles_id_seq');
            IF profile_sequence IS DISTINCT FROM 'public.customer_profiles_id_seq'
               OR profile_sequence_oid IS NULL THEN
                RAISE EXCEPTION
                    '0067 expected public.customer_profiles_id_seq for customer_profiles.id; observed %',
                    profile_sequence;
            END IF;

            -- Atlas may change only objects owned by its migration identity.
            -- Managed extension relations remain under their own owners.
            IF (
                SELECT relowner FROM pg_class WHERE oid = profile_table_oid
            ) IS DISTINCT FROM migration_role_oid THEN
                RAISE EXCEPTION
                    '0067 requires the migration identity to own public.customer_profiles';
            END IF;

            IF (
                SELECT relowner FROM pg_class WHERE oid = profile_sequence_oid
            ) IS DISTINCT FROM migration_role_oid THEN
                RAISE EXCEPTION
                    '0067 requires the migration identity to own public.customer_profiles_id_seq';
            END IF;

            IF NOT has_schema_privilege('service_role', 'public', 'USAGE') THEN
                RAISE EXCEPTION
                    'service_role must retain USAGE on public for its explicitly granted customer_profiles access';
            END IF;

            -- A one-time object revoke cannot protect objects created later.
            -- Check global and public-schema defaults for direct service_role
            -- grants. This includes independent Supabase-managed defaults.
            SELECT owner_role.rolname, defaults.defaclobjtype
            INTO default_acl_owner, default_acl_object_type
            FROM pg_default_acl AS defaults
            JOIN pg_roles AS owner_role
              ON owner_role.oid = defaults.defaclrole
            CROSS JOIN LATERAL aclexplode(defaults.defaclacl) AS acl
            WHERE defaults.defaclnamespace IN (0, public_schema_oid)
              AND acl.grantee = service_role_oid
              AND (
                    (defaults.defaclobjtype = 'r' AND acl.privilege_type IN (
                        'SELECT', 'INSERT', 'UPDATE', 'DELETE', 'TRUNCATE',
                        'REFERENCES', 'TRIGGER', 'MAINTAIN'
                    ))
                    OR (defaults.defaclobjtype = 'S' AND acl.privilege_type IN (
                        'USAGE', 'SELECT', 'UPDATE'
                    ))
                    OR (defaults.defaclobjtype = 'f' AND acl.privilege_type = 'EXECUTE')
              )
            ORDER BY owner_role.rolname, defaults.defaclobjtype
            LIMIT 1;

            IF FOUND THEN
                RAISE EXCEPTION
                    '0067 refuses to continue: role % has default % privileges for service_role on future public objects',
                    default_acl_owner, default_acl_object_type
                    USING HINT =
                        'Have the default-privilege owner remove the service_role defaults using an authorized managed-owner operation, then rerun the migration. Do not bypass this gate.';
            END IF;

            -- Revoke only objects this migration identity owns. A schema-wide
            -- REVOKE also targets managed extension relations and can fail when
            -- the migration login cannot alter their owner's ACLs.
            FOR object_record IN
                SELECT c.relkind, n.nspname, c.relname
                FROM pg_class AS c
                JOIN pg_namespace AS n ON n.oid = c.relnamespace
                WHERE n.nspname = 'public'
                  AND c.relowner = migration_role_oid
                  AND c.relkind IN ('r', 'p', 'v', 'm', 'f', 'S')
            LOOP
                IF object_record.relkind = 'S' THEN
                    EXECUTE format(
                        'REVOKE ALL PRIVILEGES ON SEQUENCE %I.%I FROM service_role',
                        object_record.nspname, object_record.relname
                    );
                ELSE
                    EXECUTE format(
                        'REVOKE ALL PRIVILEGES ON TABLE %I.%I FROM service_role',
                        object_record.nspname, object_record.relname
                    );
                END IF;
            END LOOP;

            -- The Edge Function admin client performs table operations only;
            -- it does not call public RPCs. Remove legacy EXECUTE grants from
            -- Atlas-owned public routines. PostgreSQL trigger firing does not
            -- require the caller to retain EXECUTE on a trigger function.
            FOR function_record IN
                SELECT p.prokind, n.nspname, p.proname,
                       pg_get_function_identity_arguments(p.oid) AS identity_arguments
                FROM pg_proc AS p
                JOIN pg_namespace AS n ON n.oid = p.pronamespace
                WHERE n.nspname = 'public'
                  AND p.proowner = migration_role_oid
                  AND p.prokind IN ('f', 'p', 'a', 'w')
            LOOP
                EXECUTE format(
                    'REVOKE EXECUTE ON %s %I.%I(%s) FROM service_role',
                    CASE WHEN function_record.prokind = 'p'
                         THEN 'PROCEDURE' ELSE 'FUNCTION' END,
                    function_record.nspname,
                    function_record.proname,
                    function_record.identity_arguments
                );
            END LOOP;

            EXECUTE
                'GRANT SELECT, INSERT ON TABLE public.customer_profiles TO service_role';
            EXECUTE
                'GRANT USAGE, SELECT ON SEQUENCE public.customer_profiles_id_seq TO service_role';

            IF NOT has_table_privilege('service_role', profile_table_oid, 'SELECT')
               OR NOT has_table_privilege('service_role', profile_table_oid, 'INSERT')
               OR has_table_privilege('service_role', profile_table_oid, 'UPDATE')
               OR has_table_privilege('service_role', profile_table_oid, 'DELETE')
               OR has_table_privilege('service_role', profile_table_oid, 'TRUNCATE')
               OR has_table_privilege('service_role', profile_table_oid, 'REFERENCES')
               OR has_table_privilege('service_role', profile_table_oid, 'TRIGGER') THEN
                RAISE EXCEPTION
                    'service_role customer_profiles privileges do not match SELECT, INSERT only';
            END IF;

            IF EXISTS (
                SELECT 1
                FROM pg_class AS c
                JOIN pg_namespace AS n ON n.oid = c.relnamespace
                WHERE n.nspname = 'public'
                  AND c.relowner = migration_role_oid
                  AND c.relkind IN ('r', 'p', 'v', 'm', 'f')
                  AND c.oid <> profile_table_oid
                  AND (
                      has_table_privilege('service_role', c.oid, 'SELECT')
                      OR has_table_privilege('service_role', c.oid, 'INSERT')
                      OR has_table_privilege('service_role', c.oid, 'UPDATE')
                      OR has_table_privilege('service_role', c.oid, 'DELETE')
                      OR has_table_privilege('service_role', c.oid, 'TRUNCATE')
                      OR has_table_privilege('service_role', c.oid, 'REFERENCES')
                      OR has_table_privilege('service_role', c.oid, 'TRIGGER')
                  )
            ) THEN
                RAISE EXCEPTION
                    'service_role retains privileges on an Atlas-owned public relation other than customer_profiles';
            END IF;

            -- MAINTAIN is PostgreSQL 17+. Dynamic SQL keeps PostgreSQL 16
            -- from parsing an unsupported privilege name.
            IF current_setting('server_version_num')::integer >= 170000 THEN
                EXECUTE $atlas_service_role_maintain_check$
                    SELECT
                        has_table_privilege('service_role', $1, 'MAINTAIN')
                        OR EXISTS (
                            SELECT 1
                            FROM pg_class AS c
                            JOIN pg_namespace AS n ON n.oid = c.relnamespace
                            WHERE n.nspname = 'public'
                              AND c.relowner = $2
                              AND c.relkind IN ('r', 'p', 'v', 'm', 'f')
                              AND c.oid <> $1
                              AND has_table_privilege(
                                  'service_role', c.oid, 'MAINTAIN'
                              )
                        )
                $atlas_service_role_maintain_check$
                INTO has_maintain
                USING profile_table_oid, migration_role_oid;

                IF has_maintain THEN
                    RAISE EXCEPTION
                        'service_role retains MAINTAIN on an Atlas-owned public relation';
                END IF;
            END IF;

            IF NOT has_sequence_privilege(
                    'service_role', profile_sequence_oid, 'USAGE'
               )
               OR NOT has_sequence_privilege(
                    'service_role', profile_sequence_oid, 'SELECT'
               )
               OR has_sequence_privilege(
                    'service_role', profile_sequence_oid, 'UPDATE'
               ) THEN
                RAISE EXCEPTION
                    'service_role customer_profiles_id_seq privileges do not match USAGE, SELECT only';
            END IF;

            IF EXISTS (
                SELECT 1
                FROM pg_proc AS p
                JOIN pg_namespace AS n ON n.oid = p.pronamespace
                WHERE n.nspname = 'public'
                  AND has_function_privilege('service_role', p.oid, 'EXECUTE')
            ) THEN
                RAISE EXCEPTION
                    'service_role retains EXECUTE on a public function; remove direct or managed-owner grants before enabling the Edge Function admin client';
            END IF;

            IF EXISTS (
                SELECT 1
                FROM pg_class AS c
                JOIN pg_namespace AS n ON n.oid = c.relnamespace
                WHERE n.nspname = 'public'
                  AND c.relowner = migration_role_oid
                  AND c.relkind = 'S'
                  AND c.oid <> profile_sequence_oid
                  AND (
                      has_sequence_privilege('service_role', c.oid, 'USAGE')
                      OR has_sequence_privilege('service_role', c.oid, 'SELECT')
                      OR has_sequence_privilege('service_role', c.oid, 'UPDATE')
                  )
            ) THEN
                RAISE EXCEPTION
                    'service_role retains privileges on an Atlas-owned public sequence other than customer_profiles_id_seq';
            END IF;
        END
        $atlas_service_role_public_acl$;
    """))


def downgrade() -> None:
    # Deliberately forward-only: do not restore broad Supabase service_role ACLs.
    pass
