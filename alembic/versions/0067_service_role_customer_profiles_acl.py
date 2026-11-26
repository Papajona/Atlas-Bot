"""Restrict Supabase service_role public table and sequence grants.

Revision ID: 0067_service_role_customer_profiles_acl
Revises: 0066_pin_research_trigger_path

The built-in service_role keeps only the explicitly requested profile-table
access. Atlas application runtimes must continue using their dedicated roles.
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
            profile_sequence text;
        BEGIN
            -- Local and non-Supabase PostgreSQL installations may not define
            -- Supabase's managed service_role. Do not create or impersonate it.
            IF NOT EXISTS (
                SELECT 1 FROM pg_roles WHERE rolname = 'service_role'
            ) THEN
                RETURN;
            END IF;

            IF to_regclass('public.customer_profiles') IS NULL THEN
                RAISE EXCEPTION
                    '0067 requires public.customer_profiles before restricting service_role';
            END IF;

            IF NOT has_schema_privilege('service_role', 'public', 'USAGE') THEN
                RAISE EXCEPTION
                    'service_role must retain USAGE on public for its explicitly granted customer_profiles access';
            END IF;

            profile_sequence := pg_get_serial_sequence(
                'public.customer_profiles',
                'id'
            );
            IF profile_sequence IS DISTINCT FROM 'public.customer_profiles_id_seq' THEN
                RAISE EXCEPTION
                    '0067 expected public.customer_profiles_id_seq for customer_profiles.id; observed %',
                    profile_sequence;
            END IF;

            -- Remove existing object-level grants first. Revision 0053 already
            -- removes future default grants for the migration object owner.
            EXECUTE
                'REVOKE ALL PRIVILEGES ON ALL TABLES IN SCHEMA public FROM service_role';
            EXECUTE
                'REVOKE ALL PRIVILEGES ON ALL SEQUENCES IN SCHEMA public FROM service_role';

            EXECUTE
                'GRANT SELECT, INSERT ON TABLE public.customer_profiles TO service_role';
            EXECUTE
                'GRANT USAGE, SELECT ON SEQUENCE public.customer_profiles_id_seq TO service_role';

            -- Fail the migration if inherited/PUBLIC grants or an unexpected
            -- ownership/default-ACL state leave service_role with extra access.
            IF NOT has_table_privilege(
                    'service_role', 'public.customer_profiles', 'SELECT'
               )
               OR NOT has_table_privilege(
                    'service_role', 'public.customer_profiles', 'INSERT'
               )
               OR has_table_privilege(
                    'service_role', 'public.customer_profiles', 'UPDATE'
               )
               OR has_table_privilege(
                    'service_role', 'public.customer_profiles', 'DELETE'
               )
               OR has_table_privilege(
                    'service_role', 'public.customer_profiles', 'TRUNCATE'
               )
               OR has_table_privilege(
                    'service_role', 'public.customer_profiles', 'REFERENCES'
               )
               OR has_table_privilege(
                    'service_role', 'public.customer_profiles', 'TRIGGER'
               )
               OR has_table_privilege(
                    'service_role', 'public.customer_profiles', 'MAINTAIN'
               ) THEN
                RAISE EXCEPTION
                    'service_role customer_profiles privileges do not match SELECT, INSERT only';
            END IF;

            IF EXISTS (
                SELECT 1
                FROM pg_class c
                JOIN pg_namespace n ON n.oid = c.relnamespace
                WHERE n.nspname = 'public'
                  AND c.relkind IN ('r', 'p', 'v', 'm', 'f')
                  AND c.oid <> to_regclass('public.customer_profiles')
                  AND (
                      has_table_privilege('service_role', c.oid, 'SELECT')
                      OR has_table_privilege('service_role', c.oid, 'INSERT')
                      OR has_table_privilege('service_role', c.oid, 'UPDATE')
                      OR has_table_privilege('service_role', c.oid, 'DELETE')
                      OR has_table_privilege('service_role', c.oid, 'TRUNCATE')
                      OR has_table_privilege('service_role', c.oid, 'REFERENCES')
                      OR has_table_privilege('service_role', c.oid, 'TRIGGER')
                      OR has_table_privilege('service_role', c.oid, 'MAINTAIN')
                  )
            ) THEN
                RAISE EXCEPTION
                    'service_role retains privileges on a public relation other than customer_profiles';
            END IF;

            IF NOT has_sequence_privilege(
                    'service_role', 'public.customer_profiles_id_seq', 'USAGE'
               )
               OR NOT has_sequence_privilege(
                    'service_role', 'public.customer_profiles_id_seq', 'SELECT'
               )
               OR has_sequence_privilege(
                    'service_role', 'public.customer_profiles_id_seq', 'UPDATE'
               ) THEN
                RAISE EXCEPTION
                    'service_role customer_profiles_id_seq privileges do not match USAGE, SELECT only';
            END IF;

            IF EXISTS (
                SELECT 1
                FROM pg_class c
                JOIN pg_namespace n ON n.oid = c.relnamespace
                WHERE n.nspname = 'public'
                  AND c.relkind = 'S'
                  AND c.oid <> to_regclass('public.customer_profiles_id_seq')
                  AND (
                      has_sequence_privilege('service_role', c.oid, 'USAGE')
                      OR has_sequence_privilege('service_role', c.oid, 'SELECT')
                      OR has_sequence_privilege('service_role', c.oid, 'UPDATE')
                  )
            ) THEN
                RAISE EXCEPTION
                    'service_role retains privileges on a public sequence other than customer_profiles_id_seq';
            END IF;
        END
        $atlas_service_role_public_acl$;
    """))


def downgrade() -> None:
    # Deliberately forward-only: do not restore broad Supabase service_role ACLs.
    pass
