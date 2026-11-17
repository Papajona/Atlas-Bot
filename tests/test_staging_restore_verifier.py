from pathlib import Path
import os

import pytest

from scripts.verify_staging_restore import read_database_uri, required_table_failures, truthy, validate_cloud_sql_target_pair


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
    assert "no second project or paid branch is required" in runbook
    # Same-project staging and read-only reconciliation are allowed, but a full
    # backup restore may never target the active production database.
    assert 'if [[ "$RESTORE_TARGET_KIND" == "supabase" ]]; then' in workflow
    assert '[[ "$STAGING_SUPABASE_PROJECT_REF" != "$PRODUCTION_SUPABASE_PROJECT_REF" ]]' in workflow
    assert '[[ "$STAGING_SUPABASE_PROJECT_REF" != "$PRODUCTION_SUPABASE_PROJECT_REF" ]]' not in identity_workflow
    assert '[[ "$STAGING_SUPABASE_PROJECT_REF" != "$PRODUCTION_SUPABASE_PROJECT_REF" ]]' not in reconciliation_workflow
    assert "restore audit target" in runbook
    assert "The current Free single-project setup has no verified separate restore target" in runbook
    assert "full restore certification" in runbook
    assert "PRODUCTION_CLOUD_SQL_INSTANCE_CONNECTION_NAME" in workflow
    assert "backup reference must pin an exact GCS generation" in workflow
    assert 'gcloud sql operations describe "$RESTORE_OPERATION_ID"' in workflow
    assert "verify_cloud_sql_backup_binding.py" in workflow
    assert '"versions": "true"' in workflow
    assert 'objects describe "$BACKUP_REFERENCE"' in workflow
    assert '--target-database "$CLOUD_SQL_DATABASE"' in workflow




def test_restore_audit_enforces_column_scoped_read_only_audit_identity():
    import re

    from scripts.verify_staging_restore import (
        AUDIT_REQUIRED_SELECT_COLUMNS,
        QUERY,
        audit_role_failures,
    )

    verifier = Path("scripts/verify_staging_restore.py").read_text(encoding="utf-8")
    provisioner = Path("scripts/provision_atlas_audit_reader.sql").read_text(encoding="utf-8")
    normalized_provisioner = re.sub(r"\s+", " ", provisioner)

    assert "'required_select_missing'" in QUERY
    assert "'unexpected_select'" in QUERY
    assert "'public_tables_with_write_privileges'" in QUERY
    assert "'public_sequences_with_privileges'" in QUERY
    assert "'public_functions_with_execute'" in QUERY
    assert "has_column_privilege" in QUERY
    assert "has_table_privilege(r.rolname,c.oid,'TRIGGER')" in QUERY
    assert "has_table_privilege(r.rolname,c.oid,'REFERENCES')" in QUERY
    assert "has_table_privilege(r.rolname,c.oid,'TRUNCATE')" in QUERY
    assert "expected audit role must be exactly {AUDIT_ROLE_NAME}" in verifier
    assert "expected audit role must be exactly atlas_audit_reader" in audit_role_failures(
        {}, "wrong_role", "on"
    )

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
        "role_default_read_only": True,
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
        "audit connection is not running in a read-only PostgreSQL transaction" in failure
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


def test_restore_audit_accepts_cloud_sql_auth_proxy_target():
    env, host = read_database_uri(
        "postgresql://atlas_audit_reader:unit-test-password@127.0.0.1:5432/atlas_recovery",
        target_kind="cloud_sql", target_database="atlas-recovery-db",
        cloud_sql_instance="atlas-staging-510915:europe-west1:atlas-recovery",
        cloud_sql_database="atlas_recovery",
    )
    assert host == "127.0.0.1"
    assert env["PGDATABASE"] == "atlas_recovery"
    assert env["PGOPTIONS"] == "-c default_transaction_read_only=on -c statement_timeout=15000"
    assert env["PGSSLMODE"] == "disable"  # TLS terminates in the Cloud SQL Auth Proxy


@pytest.mark.parametrize("dsn", [
    "postgresql://user:pw@db.production.example:5432/atlas_recovery",
    "postgresql://user:pw@127.0.0.1:5432/postgres",
])
def test_restore_audit_rejects_wrong_cloud_sql_host_or_database(dsn):
    with pytest.raises(ValueError):
        read_database_uri(
            dsn, target_kind="cloud_sql", target_database="atlas-recovery-db",
            cloud_sql_instance="atlas-staging-510915:europe-west1:atlas-recovery",
            cloud_sql_database="atlas_recovery",
        )


def test_restore_audit_rejects_malformed_cloud_sql_instance():
    with pytest.raises(ValueError, match="project:region:instance"):
        read_database_uri(
            "postgresql://user:pw@127.0.0.1:5432/atlas_recovery",
            target_kind="cloud_sql", target_database="atlas-recovery-db", cloud_sql_instance="production",
            cloud_sql_database="atlas_recovery",
        )


def test_cloud_sql_target_pair_requires_explicit_distinct_production_instance():
    target = "atlas-staging-510915:europe-west1:atlas-recovery"
    production = "atlas-production:europe-west1:atlas-primary"
    validate_cloud_sql_target_pair(target, production)
    with pytest.raises(ValueError, match="PRODUCTION_CLOUD_SQL_INSTANCE_CONNECTION_NAME"):
        validate_cloud_sql_target_pair(target, "")
    with pytest.raises(ValueError, match="PRODUCTION_CLOUD_SQL_INSTANCE_CONNECTION_NAME"):
        validate_cloud_sql_target_pair(target, "production")
    with pytest.raises(ValueError, match="distinct"):
        validate_cloud_sql_target_pair(target, target)


def test_cloud_sql_restore_evidence_requires_pinned_import_and_exact_instance():
    from scripts.verify_restore_operation_evidence import verify

    backup = "gs://atlas-staging-510915-db-backups/backup.dump#1750000000000000"
    document = {
        "operation_id": "restore-op-123",
        "backup_reference": backup,
        "instance_connection_name": "atlas-staging-510915:europe-west1:atlas-recovery",
        "operationType": "IMPORT",
        "status": "DONE",
        "importContext": {"database": "atlas-recovery-db", "uri": backup},
        "error": {"errors": []},
    }
    result = verify(
        document, operation_id="restore-op-123",
        backup_reference=backup,
        target_kind="cloud_sql", target_database="atlas-recovery-db",
        target_instance_connection_name="atlas-staging-510915:europe-west1:atlas-recovery",
    )
    assert result["status"] == "PASS"
    assert result["target_kind"] == "cloud_sql"
    assert result["backup_generation"] == "1750000000000000"
    assert result["operation_type"] == "IMPORT"


def test_cloud_sql_restore_evidence_rejects_wrong_instance_or_running_operation():
    from scripts.verify_restore_operation_evidence import verify

    document = {
        "operation_id": "restore-op-123",
        "backup_reference": "gs://bucket/backup.dump#1750000000000000",
        "instance_connection_name": "prod:region:database",
        "operationType": "IMPORT",
        "status": "RUNNING",
    }
    with pytest.raises(ValueError):
        verify(
            document, operation_id="restore-op-123",
            backup_reference="gs://bucket/backup.dump#1750000000000000",
            target_kind="cloud_sql", target_database="atlas-recovery-db",
            target_instance_connection_name="atlas-staging-510915:europe-west1:atlas-recovery",
        )


def test_supabase_restore_evidence_compatibility():
    from scripts.verify_restore_operation_evidence import verify

    document = {
        "operation_id": "restore-op-123", "backup_reference": "backup-123",
        "target_project_ref": "abcdefghijklmnopqrst", "status": "COMPLETED",
    }
    result = verify(
        document, operation_id="restore-op-123", backup_reference="backup-123",
        target_kind="supabase", target_project_ref="abcdefghijklmnopqrst",
    )
    assert result["target_supabase_project_ref"] == "abcdefghijklmnopqrst"


def test_cloud_sql_native_import_operation_evidence_shape():
    from scripts.verify_restore_operation_evidence import verify

    operation = {
        "name": "operation-987",
        "targetProject": "atlas-staging-510915",
        "targetId": "atlas-recovery",
        "region": "europe-west1",
        "operationType": "IMPORT",
        "status": "DONE",
        "importContext": {"database": "atlas-recovery-db", "uri": "gs://atlas-staging-510915-db-backups/atlas-prod-backup.dump#1750000000000000"},
    }
    result = verify(
        operation,
        operation_id="operation-987",
        backup_reference="gs://atlas-staging-510915-db-backups/atlas-prod-backup.dump#1750000000000000",
        target_kind="cloud_sql", target_database="atlas-recovery-db",
        target_instance_connection_name="atlas-staging-510915:europe-west1:atlas-recovery",
    )
    assert result["provider_status"] == "DONE"


def test_cloud_sql_evidence_rejects_export_or_missing_operation_type():
    from scripts.verify_restore_operation_evidence import verify

    backup = "gs://atlas-staging-510915-db-backups/backup.dump#1750000000000000"
    base = {
        "operation_id": "restore-op-123",
        "backup_reference": backup,
        "instance_connection_name": "atlas-staging-510915:europe-west1:atlas-recovery",
        "status": "DONE",
    }
    for document in (base, {**base, "operationType": "EXPORT"}):
        with pytest.raises(ValueError, match="IMPORT"):
            verify(
                document, operation_id="restore-op-123", backup_reference=backup,
                target_kind="cloud_sql", target_database="atlas-recovery-db",
                target_instance_connection_name="atlas-staging-510915:europe-west1:atlas-recovery",
            )


def test_cloud_sql_done_import_with_provider_errors_is_rejected():
    from scripts.verify_restore_operation_evidence import verify

    backup = "gs://atlas-staging-510915-db-backups/backup.dump#1750000000000000"
    document = {
        "operation_id": "restore-op-123",
        "backup_reference": backup,
        "instance_connection_name": "atlas-staging-510915:europe-west1:atlas-recovery",
        "operationType": "IMPORT",
        "status": "DONE",
        "error": {"errors": [{"code": "INVALID_ARGUMENT", "message": "import rejected"}]},
    }
    with pytest.raises(ValueError, match="provider errors"):
        verify(
            document, operation_id="restore-op-123", backup_reference=backup,
            target_kind="cloud_sql", target_database="atlas-recovery-db",
            target_instance_connection_name="atlas-staging-510915:europe-west1:atlas-recovery",
        )


def test_cloud_sql_native_aliases_cannot_hide_conflicting_target_or_backup():
    from scripts.verify_restore_operation_evidence import verify

    target = "atlas-staging-510915:europe-west1:atlas-recovery"
    backup = "gs://atlas-staging-510915-db-backups/backup.dump#1750000000000000"
    common = {
        "operation_id": "restore-op-123",
        "instance_connection_name": target,
        "operationType": "IMPORT",
        "status": "DONE",
    }
    wrong_target = {
        **common,
        "targetProject": "atlas-production",
        "region": "europe-west1",
        "targetId": "atlas-primary",
        "importContext": {"database": "atlas-recovery-db", "uri": backup},
    }
    with pytest.raises(ValueError, match="conflicting Cloud SQL target"):
        verify(
            wrong_target, operation_id="restore-op-123", backup_reference=backup,
            target_kind="cloud_sql", target_database="atlas-recovery-db", target_instance_connection_name=target,
        )

    wrong_backup = {
        **common,
        "backup_reference": backup,
        "importContext": {"database": "atlas-recovery-db", "uri": "gs://atlas-staging-510915-db-backups/other.dump#1750000000000001"},
    }
    with pytest.raises(ValueError, match="conflicting backup reference"):
        verify(
            wrong_backup, operation_id="restore-op-123", backup_reference=backup,
            target_kind="cloud_sql", target_database="atlas-recovery-db", target_instance_connection_name=target,
        )


def test_cloud_sql_restore_requires_generation_pinned_backup_reference():
    from scripts.verify_restore_operation_evidence import verify

    document = {
        "operation_id": "restore-op-123",
        "backup_reference": "gs://atlas-staging-510915-db-backups/backup.dump",
        "instance_connection_name": "atlas-staging-510915:europe-west1:atlas-recovery",
        "operationType": "IMPORT",
        "status": "DONE",
    }
    with pytest.raises(ValueError, match="pin an immutable GCS object generation"):
        verify(
            document, operation_id="restore-op-123",
            backup_reference="gs://atlas-staging-510915-db-backups/backup.dump",
            target_kind="cloud_sql", target_database="atlas-recovery-db",
            target_instance_connection_name="atlas-staging-510915:europe-west1:atlas-recovery",
        )


def test_nested_supabase_restore_record_is_selected_over_targetless_wrapper_metadata():
    from scripts.verify_restore_operation_evidence import verify

    document = {
        "id": "wrapper-request-id",
        "backup_reference": "wrapper-backup",
        "status": "COMPLETED",
        "data": {
            "result": {
                "operation": {
                    "operation_id": "restore-op-123",
                    "backup_reference": "backup-123",
                    "target_project_ref": "abcdefghijklmnopqrst",
                    "status": "COMPLETED",
                }
            }
        },
    }
    result = verify(
        document, operation_id="restore-op-123", backup_reference="backup-123",
        target_kind="supabase", target_project_ref="abcdefghijklmnopqrst",
    )
    assert result["status"] == "PASS"
    assert result["target_supabase_project_ref"] == "abcdefghijklmnopqrst"


def test_cloud_sql_native_operation_without_region_uses_configured_instance_metadata():
    from scripts.verify_restore_operation_evidence import verify

    pinned = "gs://atlas-staging-510915-db-backups/backup.dump#1750000000000000"
    operation = {
        "name": "operation-987",
        "targetProject": "atlas-staging-510915",
        "targetId": "atlas-recovery",
        "operationType": "IMPORT",
        "status": "DONE",
        "importContext": {"database": "atlas-recovery-db", "uri": "gs://atlas-staging-510915-db-backups/backup.dump"},
    }
    result = verify(
        operation,
        operation_id="operation-987",
        backup_reference=pinned,
        target_kind="cloud_sql", target_database="atlas-recovery-db",
        target_instance_connection_name="atlas-staging-510915:europe-west1:atlas-recovery",
    )
    assert result["backup_generation"] == "1750000000000000"
    assert result["native_import_uri_has_generation"] is False
    assert result["target_instance_connection_name"] == "atlas-staging-510915:europe-west1:atlas-recovery"


def test_cloud_sql_native_import_uri_matches_object_part_not_generation_fragment():
    from scripts.verify_restore_operation_evidence import verify

    pinned = "gs://atlas-staging-510915-db-backups/backup.dump#1750000000000000"
    operation = {
        "name": "operation-987",
        "targetProject": "atlas-staging-510915",
        "targetId": "atlas-recovery",
        "operationType": "IMPORT",
        "status": "DONE",
        "importContext": {"database": "atlas-recovery-db", "uri": "gs://atlas-staging-510915-db-backups/backup.dump"},
    }
    assert verify(
        operation, operation_id="operation-987", backup_reference=pinned,
        target_kind="cloud_sql", target_database="atlas-recovery-db",
        target_instance_connection_name="atlas-staging-510915:europe-west1:atlas-recovery",
    )["backup_reference"] == pinned


def test_gcs_backup_binding_accepts_standardized_snake_case_checksum_fields():
    from scripts.verify_cloud_sql_backup_binding import verify_backup_binding

    result = verify_backup_binding(
        backup_reference="gs://atlas-staging-510915-db-backups/backup.dump#1750000000000000",
        object_metadata={
            "generation": "1750000000000000",
            "size": "1024",
            "md5_hash": "d41d8cd98f00b204e9800998ecf8427e",
            "time_created": "2026-11-17T10:00:00Z",
            "retention_expiration_time": "2027-11-17T10:00:00Z",
        },
        bucket_metadata={"retention_policy": {"is_locked": True, "retention_period": "31536000s"}},
        object_versions={"items": [{
            "name": "backup.dump", "generation": "1750000000000000",
            "timeCreated": "2026-11-17T10:00:00Z",
        }]},
        operation_record={
            "name": "operation-987",
            "operationType": "IMPORT",
            "status": "DONE",
            "startTime": "2026-11-17T10:05:00Z",
            "endTime": "2026-11-17T10:10:00Z",
            "importContext": {"database": "atlas-recovery-db", "uri": "gs://atlas-staging-510915-db-backups/backup.dump"},
        },
    )
    assert result["status"] == "PASS"
    assert result["backup_generation"] == "1750000000000000"
    assert result["bucket_retention_policy_locked"] is True
    assert result["backup_checksum"]["md5_hash"] == "d41d8cd98f00b204e9800998ecf8427e"


def test_gcs_backup_binding_rejects_generation_mismatch_and_unlocked_retention():
    from scripts.verify_cloud_sql_backup_binding import verify_backup_binding

    args = {
        "backup_reference": "gs://atlas-staging-510915-db-backups/backup.dump#1750000000000000",
        "object_metadata": {
            "generation": "1750000000000001",
            "size": "1024",
            "crc32c_hash": "ImIEBA==",
            "time_created": "2026-11-17T10:00:00Z",
            "retention_expiration_time": "2027-11-17T10:00:00Z",
        },
        "bucket_metadata": {"retention_policy": {"is_locked": False}},
        "object_versions": {"items": [{
            "name": "backup.dump", "generation": "1750000000000000",
            "timeCreated": "2026-11-17T10:00:00Z",
        }]},
        "operation_record": {
            "operationType": "IMPORT", "status": "DONE",
            "startTime": "2026-11-17T10:05:00Z", "endTime": "2026-11-17T10:10:00Z",
        },
    }
    with pytest.raises(ValueError, match="generation"):
        verify_backup_binding(**args)
    args["object_metadata"]["generation"] = "1750000000000000"
    with pytest.raises(ValueError, match="retention policy must be locked"):
        verify_backup_binding(**args)


def test_gcs_backup_binding_rejects_retention_not_covering_import_window():
    from scripts.verify_cloud_sql_backup_binding import verify_backup_binding

    with pytest.raises(ValueError, match="retained from before import start"):
        verify_backup_binding(
            backup_reference="gs://atlas-staging-510915-db-backups/backup.dump#1750000000000000",
            object_metadata={
                "generation": "1750000000000000", "size": "1024", "crc32c_hash": "ImIEBA==",
                "time_created": "2026-11-17T10:06:00Z",
                "retention_expiration_time": "2026-11-17T10:09:00Z",
            },
            bucket_metadata={"retention_policy": {"is_locked": True}},
            object_versions={"items": [{
                "name": "backup.dump", "generation": "1750000000000000",
                "timeCreated": "2026-11-17T10:06:00Z",
            }]},
            operation_record={
                "operationType": "IMPORT", "status": "DONE",
                "startTime": "2026-11-17T10:05:00Z", "endTime": "2026-11-17T10:10:00Z",
            },
        )


def test_cloud_sql_native_generation_suffixed_import_uri_is_accepted():
    from scripts.verify_restore_operation_evidence import verify

    pinned = "gs://atlas-staging-510915-db-backups/backup.dump#1750000000000000"
    operation = {
        "name": "operation-987",
        "targetProject": "atlas-staging-510915",
        "targetId": "atlas-recovery",
        "operationType": "IMPORT",
        "status": "DONE",
        "importContext": {
            "database": "atlas-recovery-db",
            "uri": pinned,
        },
    }
    assert verify(
        operation,
        operation_id="operation-987",
        backup_reference=pinned,
        target_kind="cloud_sql",
        target_instance_connection_name="atlas-staging-510915:europe-west1:atlas-recovery",
        target_database="atlas-recovery-db",
    )["backup_generation"] == "1750000000000000"


def test_cloud_sql_import_into_different_database_is_rejected():
    from scripts.verify_restore_operation_evidence import verify

    pinned = "gs://atlas-staging-510915-db-backups/backup.dump#1750000000000000"
    operation = {
        "name": "operation-987",
        "targetProject": "atlas-staging-510915",
        "targetId": "atlas-recovery",
        "operationType": "IMPORT",
        "status": "DONE",
        "importContext": {
            "database": "wrong_database",
            "uri": "gs://atlas-staging-510915-db-backups/backup.dump",
        },
    }
    with pytest.raises(ValueError, match="does not match the configured Cloud SQL database"):
        verify(
            operation, operation_id="operation-987", backup_reference=pinned,
            target_kind="cloud_sql",
            target_instance_connection_name="atlas-staging-510915:europe-west1:atlas-recovery",
            target_database="atlas-recovery-db",
        )


def test_cloud_sql_import_without_definitive_database_is_rejected():
    from scripts.verify_restore_operation_evidence import verify

    pinned = "gs://atlas-staging-510915-db-backups/backup.dump#1750000000000000"
    operation = {
        "name": "operation-987",
        "targetProject": "atlas-staging-510915",
        "targetId": "atlas-recovery",
        "operationType": "IMPORT",
        "status": "DONE",
        "importContext": {"uri": "gs://atlas-staging-510915-db-backups/backup.dump"},
    }
    with pytest.raises(ValueError, match="does not identify importContext.database"):
        verify(
            operation, operation_id="operation-987", backup_reference=pinned,
            target_kind="cloud_sql",
            target_instance_connection_name="atlas-staging-510915:europe-west1:atlas-recovery",
            target_database="atlas-recovery-db",
        )


def test_pinned_older_backup_generation_succeeds_when_newer_version_appears_after_import():
    from scripts.verify_cloud_sql_backup_binding import verify_backup_binding

    result = verify_backup_binding(
        backup_reference="gs://atlas-staging-510915-db-backups/backup.dump#1750000000000000",
        object_metadata={
            "name": "backup.dump", "bucket": "atlas-staging-510915-db-backups",
            "generation": "1750000000000000", "size": "1024",
            "crc32c_hash": "ImIEBA==", "time_created": "2026-11-17T10:00:00Z",
            "retention_expiration_time": "2027-11-17T10:00:00Z",
        },
        bucket_metadata={"retention_policy": {"is_locked": True}},
        object_versions={"items": [
            {"name": "backup.dump", "generation": "1750000000000000", "timeCreated": "2026-11-17T10:00:00Z"},
            {"name": "backup.dump", "generation": "1750000000000001", "timeCreated": "2026-11-17T10:20:00Z"},
        ]},
        operation_record={
            "operationType": "IMPORT", "status": "DONE",
            "startTime": "2026-11-17T10:05:00Z", "endTime": "2026-11-17T10:10:00Z",
        },
    )
    assert result["backup_generation"] == "1750000000000000"
    assert result["live_generation_at_import_start"] == "1750000000000000"
    assert result["live_generation_at_import_end"] == "1750000000000000"


def test_backup_binding_rejects_object_replaced_during_import_interval():
    from scripts.verify_cloud_sql_backup_binding import verify_backup_binding

    with pytest.raises(ValueError, match="live object generation for the full"):
        verify_backup_binding(
            backup_reference="gs://atlas-staging-510915-db-backups/backup.dump#1750000000000000",
            object_metadata={
                "name": "backup.dump", "bucket": "atlas-staging-510915-db-backups",
                "generation": "1750000000000000", "size": "1024",
                "crc32c_hash": "ImIEBA==", "time_created": "2026-11-17T10:00:00Z",
                "retention_expiration_time": "2027-11-17T10:00:00Z",
            },
            bucket_metadata={"retention_policy": {"is_locked": True}},
            object_versions={"items": [
                {"name": "backup.dump", "generation": "1750000000000000", "timeCreated": "2026-11-17T10:00:00Z"},
                {"name": "backup.dump", "generation": "1750000000000001", "timeCreated": "2026-11-17T10:07:00Z"},
            ]},
            operation_record={
                "operationType": "IMPORT", "status": "DONE",
                "startTime": "2026-11-17T10:05:00Z", "endTime": "2026-11-17T10:10:00Z",
            },
        )
