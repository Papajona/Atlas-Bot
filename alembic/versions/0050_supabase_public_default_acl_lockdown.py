"""Prevent new public objects from inheriting service_role access by default.

Existing object grants are intentionally preserved. Data API access to each new
public object must be reviewed and granted explicitly when it is actually needed.
"""
from alembic import op

revision = "0050_supabase_public_default_acl_lockdown"
down_revision = "0049_public_schema_rls_lockdown"
branch_labels = None
depends_on = None


def upgrade():
    # Default privileges apply to objects created by the role running this
    # migration. Do not revoke grants on existing tables: this change only
    # changes how future public objects are initialized.
    op.execute(
        """
        DO $atlas_service_role_defaults$
        BEGIN
            IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'service_role') THEN
                EXECUTE 'ALTER DEFAULT PRIVILEGES IN SCHEMA public REVOKE ALL PRIVILEGES ON TABLES FROM service_role';
                EXECUTE 'ALTER DEFAULT PRIVILEGES IN SCHEMA public REVOKE ALL PRIVILEGES ON SEQUENCES FROM service_role';
                EXECUTE 'ALTER DEFAULT PRIVILEGES IN SCHEMA public REVOKE EXECUTE ON FUNCTIONS FROM service_role';
            END IF;
        END
        $atlas_service_role_defaults$;
        """
    )


def downgrade():
    # Security posture is intentionally not weakened on rollback. Restoring
    # automatic future grants requires a separately reviewed access plan.
    pass
