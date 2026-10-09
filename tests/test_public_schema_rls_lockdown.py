from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
MIGRATION = ROOT / "alembic" / "versions" / "0024_public_schema_rls_lockdown.py"


def test_public_schema_lockdown_migration_is_chained_after_current_head():
    source = MIGRATION.read_text()
    assert 'revision = "0024_public_schema_rls_lockdown"' in source
    assert 'down_revision = "0023_security_billing_hardening"' in source


def test_public_schema_lockdown_revokes_api_roles_and_enables_rls_without_permissive_policies():
    source = MIGRATION.read_text()
    assert "REVOKE ALL PRIVILEGES ON ALL TABLES IN SCHEMA public FROM PUBLIC, anon, authenticated" in source
    assert "REVOKE ALL PRIVILEGES ON ALL SEQUENCES IN SCHEMA public FROM PUBLIC, anon, authenticated" in source
    assert "ALTER DEFAULT PRIVILEGES IN SCHEMA public REVOKE ALL PRIVILEGES ON TABLES" in source
    assert "ALTER DEFAULT PRIVILEGES IN SCHEMA public REVOKE ALL PRIVILEGES ON SEQUENCES" in source
    assert "ALTER TABLE %I.%I ENABLE ROW LEVEL SECURITY" in source
    assert "CREATE POLICY" not in source.upper()


def test_public_schema_lockdown_downgrade_does_not_restore_unsafe_access():
    source = MIGRATION.read_text()
    downgrade = source.split("def downgrade():", 1)[1]
    assert "REVOKE" not in downgrade.upper()
    assert "DISABLE ROW LEVEL SECURITY" not in downgrade.upper()
