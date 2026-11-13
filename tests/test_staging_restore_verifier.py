from pathlib import Path
import os

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
    provider_verifier = Path("scripts/verify_provider_ledger_reconciliation.py").read_text(encoding="utf-8")
    verifier = Path("scripts/verify_staging_restore.py").read_text(encoding="utf-8")
    runbook = Path("RUNBOOK.md").read_text(encoding="utf-8")
    expected = "0054_audit_log_idempotency_key"

    # Restore verification still checks the expected migration revision. The
    # isolated paper-only runtime intentionally avoids the production Alembic
    # migration path and verifies its two read-only identities plus ledger readiness.
    assert f"--expected-revision {expected}" in workflow
    assert f"--expected-revision {expected}" in reconciliation_workflow
    assert f'default="{expected}"' in verifier
    assert f'default="{expected}"' in provider_verifier
    assert f"checks for revision `{expected}`" in runbook
    assert "database revision is 0049" not in runbook
    assert "same actual ref" in runbook
    assert "EXPECTED_ALEMBIC_HEAD" not in identity_workflow
    assert "0054_audit_log_idempotency_key" not in identity_workflow
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



def test_restore_audit_requires_research_integrity_tables_and_valid_retained_state():
    from pathlib import Path

    verifier = Path("scripts/verify_staging_restore.py").read_text(encoding="utf-8")
    assert "'research_trial_ledger_head'" in verifier
    assert "'research_holdout_registry'" in verifier
    assert "research_trial_ledger_head_rows" in verifier
    assert "research_trial_ledger_head_valid" in verifier
    assert "research_holdout_registry_invalid_rows" in verifier

    assert required_table_failures([
        "research_trial_ledger_head", "research_holdout_registry"
    ]) == [
        "required staging table missing: research_trial_ledger_head",
        "required staging table missing: research_holdout_registry",
    ]



def test_restore_integrity_values_are_populated_from_sql_query_output():
    import json
    from types import SimpleNamespace
    from scripts.verify_staging_restore import (
        QUERY,
        RESEARCH_INTEGRITY_QUERY,
        add_research_integrity_observations,
    )

    observed = {
        "research_trial_ledger_head_present": True,
        "research_holdout_registry_present": True,
    }
    query_result = {
        "research_trial_ledger_head_rows": 1,
        "research_trial_ledger_head_valid": True,
        "research_holdout_registry_invalid_rows": 0,
    }
    calls = []

    def fake_run(command, **kwargs):
        calls.append(command)
        assert command[-1] == RESEARCH_INTEGRITY_QUERY
        return SimpleNamespace(returncode=0, stdout=json.dumps(query_result))

    result = add_research_integrity_observations(
        observed, env={"PGHOST": "staging-db"}, run=fake_run
    )
    assert "research_trial_ledger_head_present" in QUERY
    assert "research_holdout_registry_present" in QUERY
    assert result["research_trial_ledger_head_rows"] == 1
    assert result["research_trial_ledger_head_valid"] is True
    assert result["research_holdout_registry_invalid_rows"] == 0
    assert len(calls) == 1


def test_restore_integrity_query_skips_absent_0059_tables_without_sql_reference():
    from scripts.verify_staging_restore import add_research_integrity_observations

    observed = {
        "research_trial_ledger_head_present": False,
        "research_holdout_registry_present": False,
    }

    def should_not_run(*_args, **_kwargs):
        raise AssertionError("integrity query must not run when migration 0059 tables are absent")

    result = add_research_integrity_observations(
        observed, env={}, run=should_not_run
    )
    assert result["research_trial_ledger_head_rows"] is None
    assert result["research_trial_ledger_head_valid"] is False
    assert result["research_holdout_registry_invalid_rows"] is None

def _configured_postgres_dsn() -> str | None:
    """Return only an explicitly PostgreSQL DSN, never an application SQLite fallback."""
    for name in (
        "ATLAS_POSTGRES_URL",
        "ATLAS_PAPER_PRIMARY_TEST_DATABASE_URL",
        "ATLAS_PAPER_TEST_DATABASE_URL",
        "DATABASE_URL",
    ):
        candidate = os.getenv(name, "").strip()
        if candidate.startswith(("postgresql+asyncpg://", "postgresql://", "postgres://")):
            return candidate.replace("postgresql+asyncpg://", "postgresql://", 1)
    return None


_RESEARCH_INTEGRITY_POSTGRES_DSN = _configured_postgres_dsn()


@pytest.mark.postgres
@pytest.mark.skipif(
    _RESEARCH_INTEGRITY_POSTGRES_DSN is None,
    reason="requires the migrated disposable PostgreSQL database provisioned by CI",
)
def test_research_integrity_query_executes_against_postgres():
    import asyncio
    import json
    import asyncpg
    from scripts.verify_staging_restore import RESEARCH_INTEGRITY_QUERY

    dsn = _RESEARCH_INTEGRITY_POSTGRES_DSN

    async def execute_query():
        connection = await asyncpg.connect(dsn)
        try:
            return await connection.fetchval(RESEARCH_INTEGRITY_QUERY)
        finally:
            await connection.close()

    result = json.loads(asyncio.run(execute_query()))
    assert result["research_trial_ledger_head_rows"] == 1
    assert result["research_trial_ledger_head_valid"] is True
    assert result["research_holdout_registry_invalid_rows"] == 0
