from pathlib import Path

import pytest

from scripts.verify_staging_restore import read_database_uri, required_table_failures, truthy


STAGING = "abcdefghijklmnopqrst"
PRODUCTION = "uvwxyzabcdefghijklmn"


def url(project):
    return f"postgresql://atlas_runtime:unit-test-password@db.{project}.supabase.co:5432/postgres"


def test_restore_audit_accepts_explicit_staging_host():
    env, host = read_database_uri(url(STAGING), STAGING, PRODUCTION)
    assert host == f"db.{STAGING}.supabase.co"
    assert env["PGOPTIONS"] == "-c default_transaction_read_only=on -c statement_timeout=15000"
    assert env["PGSSLMODE"] == "require"


def test_restore_audit_accepts_same_project_free_staging():
    env, host = read_database_uri(url(STAGING), STAGING, STAGING)
    assert host == f"db.{STAGING}.supabase.co"
    assert env["PGSSLMODE"] == "require"


def test_restore_audit_rejects_production_host():
    with pytest.raises(ValueError, match="does not match"):
        read_database_uri(url(PRODUCTION), STAGING, PRODUCTION)


def test_restore_audit_still_rejects_url_for_unconfigured_project():
    with pytest.raises(ValueError, match="does not match"):
        read_database_uri(url(PRODUCTION), STAGING, STAGING)


def test_postgres_boolean_parser_accepts_actual_psql_output():
    assert truthy("t") is True
    assert truthy("true") is True
    assert truthy("f") is False
    assert truthy("false") is False


def test_restore_revision_matches_workflow_and_operator_runbook():
    workflow = Path(".github/workflows/staging-restore-audit.yml").read_text(encoding="utf-8")
    identity_workflow = Path(".github/workflows/staging-identity-preflight.yml").read_text(encoding="utf-8")
    reconciliation_workflow = Path(".github/workflows/provider-ledger-reconciliation.yml").read_text(encoding="utf-8")
    verifier = Path("scripts/verify_staging_restore.py").read_text(encoding="utf-8")
    runbook = Path("RUNBOOK.md").read_text(encoding="utf-8")
    expected = "0053_supabase_public_default_acl_lockdown"

    # Restore verification still checks the expected migration revision. The
    # isolated paper-only runtime intentionally avoids the production Alembic
    # migration path and verifies its two read-only identities plus ledger readiness.
    assert f"--expected-revision {expected}" in workflow
    assert f'default="{expected}"' in verifier
    assert f"checks for revision `{expected}`" in runbook
    assert "database revision is 0049" not in runbook
    assert "same actual ref" in runbook
    assert "EXPECTED_ALEMBIC_HEAD" not in identity_workflow
    assert "0053_supabase_public_default_acl_lockdown" not in identity_workflow
    assert "app.paper_staging:app" in identity_workflow
    assert "can access only customer_profiles/app_state" in identity_workflow
    assert "no second project or paid branch is required" in runbook
    assert "project refs differ before restoring" not in runbook
    assert "must use a different Supabase project from production" not in identity_workflow
    for source in (workflow, identity_workflow, reconciliation_workflow):
        assert '[[ "$STAGING_SUPABASE_PROJECT_REF" != "$PRODUCTION_SUPABASE_PROJECT_REF" ]]' not in source


def test_restore_audit_rejects_missing_required_schema_objects():
    assert required_table_failures([]) == []
    assert required_table_failures(["research_trial_archive"]) == [
        "required staging table missing: research_trial_archive"
    ]
    assert required_table_failures(None) == [
        "required_tables_missing was not reported as a JSON array"
    ]
