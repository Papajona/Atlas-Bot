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
    expected = "0059_research_integrity_controls"

    assert f"--expected-revision {expected}" in workflow
    assert f"--expected-revision {expected}" in reconciliation_workflow
    assert f'default="{expected}"' in verifier
    assert f'default="{expected}"' in provider_verifier
    assert f"verifies revision `{expected}`" in runbook
    assert "app.paper_staging:app" in identity_workflow
    assert "no second Supabase project or paid branch is required" in runbook
    # Same-project staging and read-only reconciliation are allowed, but a full
    # backup restore may never target the active production database.
    assert '[[ "$STAGING_SUPABASE_PROJECT_REF" != "$PRODUCTION_SUPABASE_PROJECT_REF" ]]' in workflow
    assert '[[ "$STAGING_SUPABASE_PROJECT_REF" != "$PRODUCTION_SUPABASE_PROJECT_REF" ]]' not in identity_workflow
    assert '[[ "$STAGING_SUPABASE_PROJECT_REF" != "$PRODUCTION_SUPABASE_PROJECT_REF" ]]' not in reconciliation_workflow
    assert "A restore audit target must be a distinct non-production Supabase project reference" in runbook
    assert "the current Free single-project setup has no verified separate restore target" in runbook
    assert "full restore certification" in runbook




def test_restore_audit_enforces_column_scoped_read_only_audit_identity():
    import re

    from scripts.verify_staging_restore import (
        AUDIT_REQUIRED_SELECT_COLUMNS,
        QUERY,
        audit_role_failures,
    )

    verifier = Path("scripts/verify_staging_restore.py").read_text(encoding="utf-8")
    provisioner = Path("scripts/provision_atlas_audit_reader.sql").read_text(encoding="utf-8")
    normalized_provisioner = re.sub(r"\\s+", " ", provisioner)

    assert "'required_select_missing'" in QUERY
    assert "'unexpected_select'" in QUERY
    assert "'public_tables_with_write_privileges'" in QUERY
    assert "'public_sequences_with_privileges'" in QUERY
    assert "'public_functions_with_execute'" in QUERY
    assert "has_column_privilege" in QUERY
    assert "has_table_privilege(r.rolname,c.oid,'TRIGGER')" in QUERY
    assert "has_table_privilege(r.rolname,c.oid,'REFERENCES')" in QUERY
    assert "has_table_privilege(r.rolname,c.oid,'TRUNCATE')" in QUERY
    assert "audit role must be exactly atlas_audit_reader" in verifier

    # The verifier's allowlist and the role-provisioning SQL must be identical.
    # Table-level SELECT is deliberately absent to keep unrelated columns private.
    for table_name, columns in AUDIT_REQUIRED_SELECT_COLUMNS.items():
        statement = (
            f"GRANT SELECT ({', '.join(columns)}) ON TABLE public.{table_name} "
            "TO atlas_audit_reader;"
        )
        assert statement in normalized_provisioner
    assert "GRANT SELECT ON TABLE" not in normalized_provisioner
    assert "ALTER ROLE atlas_audit_reader SET default_transaction_read_only = on" in normalized_provisioner
    assert "CREATE ROLE atlas_audit_reader LOGIN NOINHERIT BYPASSRLS" in normalized_provisioner

    good_role = {
        "name": "atlas_audit_reader",
        "login": True,
        "inherit": False,
        "superuser": False,
        "create_role": False,
        "create_database": False,
        "replication": False,
        "bypass_rls": True,
        "owns_public_tables": 0,
        "role_memberships": 0,
        "public_schema_usage": True,
        "public_schema_create": False,
        "required_select_missing": 0,
        "unexpected_select": 0,
        "public_tables_with_write_privileges": 0,
        "public_sequences_with_privileges": 0,
        "public_functions_with_execute": 0,
    }
    assert audit_role_failures(good_role, "atlas_audit_reader", "on") == []

    unsafe_role = {**good_role, "public_tables_with_write_privileges": 1}
    assert any(
        "public_tables_with_write_privileges expected 0" in failure
        for failure in audit_role_failures(unsafe_role, "atlas_audit_reader", "on")
    )
    assert any(
        "transaction is not running in a read-only" in failure
        for failure in audit_role_failures(good_role, "atlas_audit_reader", "off")
    )
    assert any(
        "expected audit role must be exactly atlas_audit_reader" in failure
        for failure in audit_role_failures(good_role, "atlas_api_runtime", "on")
    )


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
