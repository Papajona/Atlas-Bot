from pathlib import Path

import pytest

from scripts.verify_staging_restore import read_database_uri, truthy


STAGING = "abcdefghijklmnopqrst"
PRODUCTION = "uvwxyzabcdefghijklmn"


def url(project):
    return f"postgresql://atlas_runtime:unit-test-password@db.{project}.supabase.co:5432/postgres"


def test_restore_audit_only_accepts_explicit_distinct_staging_host():
    env, host = read_database_uri(url(STAGING), STAGING, PRODUCTION)
    assert host == f"db.{STAGING}.supabase.co"
    assert env["PGOPTIONS"] == "-c default_transaction_read_only=on -c statement_timeout=15000"
    assert env["PGSSLMODE"] == "require"


def test_restore_audit_rejects_production_host():
    with pytest.raises(ValueError, match="does not match"):
        read_database_uri(url(PRODUCTION), STAGING, PRODUCTION)


def test_restore_audit_rejects_same_staging_and_production_ref():
    with pytest.raises(ValueError, match="must differ"):
        read_database_uri(url(STAGING), STAGING, STAGING)


def test_postgres_boolean_parser_accepts_actual_psql_output():
    assert truthy("t") is True
    assert truthy("true") is True
    assert truthy("f") is False
    assert truthy("false") is False


def test_restore_revision_matches_workflow_and_operator_runbook():
    workflow = Path(".github/workflows/staging-restore-audit.yml").read_text(encoding="utf-8")
    verifier = Path("scripts/verify_staging_restore.py").read_text(encoding="utf-8")
    runbook = Path("RUNBOOK.md").read_text(encoding="utf-8")
    expected = "0051_research_trial_archive"

    assert f"--expected-revision {expected}" in workflow
    assert f'default="{expected}"' in verifier
    assert f"database revision is exactly `{expected}`" in runbook
    assert "database revision is 0049" not in runbook
