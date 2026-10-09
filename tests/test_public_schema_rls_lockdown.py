from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
MIGRATION = ROOT / "alembic" / "versions" / "0024_public_schema_rls_lockdown.py"


def test_public_schema_lockdown_migration_is_chained_after_current_head():
    source = MIGRATION.read_text()
    assert 'revision = "0024_public_schema_rls_lockdown"' in source
    assert 'down_revision = "0023_security_billing_hardening"' in source


def test_public_schema_lockdown_revokes_api_roles_and_enables_rls_without_permissive_policies():
    source = MIGRATION.read_text()
    assert "REVOKE CREATE ON SCHEMA public FROM PUBLIC" in source
    assert "REVOKE ALL PRIVILEGES ON ALL TABLES IN SCHEMA public FROM PUBLIC" in source
    assert "REVOKE ALL PRIVILEGES ON ALL SEQUENCES IN SCHEMA public FROM PUBLIC" in source
    assert "FOREACH role_name IN ARRAY ARRAY['anon', 'authenticated']" in source
    assert "ALTER DEFAULT PRIVILEGES IN SCHEMA public REVOKE ALL PRIVILEGES ON TABLES FROM PUBLIC" in source
    assert "ALTER TABLE %I.%I ENABLE ROW LEVEL SECURITY" in source
    assert "CREATE POLICY" not in source.upper()


def test_ledger_functions_have_guarded_fixed_search_paths():
    source = MIGRATION.read_text()
    assert "to_regprocedure('public.atlas_block_ledger_mutation()')" in source
    assert "to_regprocedure('public.atlas_ledger_journal_deferred_check()')" in source
    assert "to_regprocedure('public.atlas_validate_ledger_journal(integer)')" in source
    assert "SET search_path = pg_catalog, public" in source


def test_public_schema_lockdown_downgrade_does_not_restore_unsafe_access():
    source = MIGRATION.read_text()
    downgrade = source.split("def downgrade():", 1)[1]
    assert "REVOKE" not in downgrade.upper()
    assert "DISABLE ROW LEVEL SECURITY" not in downgrade.upper()
