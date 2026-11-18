from __future__ import annotations

import gzip
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

from scripts.execute_cloud_sql_restore import (
    _extract_operation_id,
    _live_backup_generation,
    _pick,
    _sql_import_suffix,
    _wait_for_operation,
    validate_backup_metadata,
    validate_restore_inputs,
)
from scripts.cloud_sql_recovery_inventory import is_source_backup_candidate


TARGET = "atlas-staging-510915:europe-west1:atlas-recovery"
PRODUCTION = "atlas-production:europe-west1:atlas-primary"
BACKUP = "gs://atlas-staging-510915-db-backups/atlas-prod-backup.sql#1750000000000000"


def test_restore_inputs_accept_only_explicit_staging_target_and_immutable_backup():
    assert validate_restore_inputs(
        project_id="atlas-staging-510915",
        region="europe-west1",
        target_instance=TARGET,
        production_instance=PRODUCTION,
        target_database="atlas-recovery-db",
        backup_reference=BACKUP,
    ) == (
        "atlas-recovery",
        "atlas-prod-backup.sql",
        "1750000000000000",
    )


@pytest.mark.parametrize(
    "overrides, message",
    [
        ({"target_instance": PRODUCTION}, "staging project"),
        ({"target_instance": "atlas-staging-510915:us-east1:atlas-recovery"}, "staging region"),
        ({"production_instance": ""}, "explicitly configured"),
        ({"production_instance": TARGET}, "different from the production"),
        ({"target_database": "bad database"}, "database name is invalid"),
        ({"backup_reference": "gs://wrong-bucket/db.sql#1750000000000000"}, "approved recovery bucket"),
        ({"backup_reference": "gs://atlas-staging-510915-db-backups/db.sql"}, "generation"),
        ({"backup_reference": "gs://atlas-staging-510915-db-backups/../other.sql#1750000000000000"}, "path is invalid"),
        ({"backup_reference": "gs://atlas-staging-510915-db-backups/restore-imports/abc.sql#1750000000000000"}, "import-staging object"),
    ],
)
def test_restore_inputs_fail_closed(overrides, message):
    values = {
        "project_id": "atlas-staging-510915",
        "region": "europe-west1",
        "target_instance": TARGET,
        "production_instance": PRODUCTION,
        "target_database": "atlas-recovery-db",
        "backup_reference": BACKUP,
    }
    values.update(overrides)
    with pytest.raises(ValueError, match=message):
        validate_restore_inputs(**values)


def test_backup_metadata_requires_exact_generation_checksum_and_locked_retention():
    now = datetime(2026, 11, 17, tzinfo=timezone.utc)
    validate_backup_metadata(
        backup_reference=BACKUP,
        object_metadata={
            "name": "atlas-prod-backup.sql",
            "bucket": "atlas-staging-510915-db-backups",
            "generation": "1750000000000000",
            "size": "12345",
            "crc32c": "AAAAAA==",
            "retentionExpirationTime": (now + timedelta(days=20)).isoformat(),
        },
        bucket_metadata={"retentionPolicy": {"isLocked": True}},
        now=now,
    )


@pytest.mark.parametrize(
    "object_change,bucket_change,message",
    [
        ({"generation": "1750000000000001"}, {}, "generation does not match"),
        ({"size": "0"}, {}, "empty"),
        ({"crc32c": None}, {}, "checksum metadata"),
        ({}, {"retentionPolicy": {"isLocked": False}}, "must be locked"),
        ({"retentionExpirationTime": "2026-11-16T00:00:00+00:00"}, {}, "already expired"),
    ],
)
def test_backup_metadata_rejects_unpinned_or_unretained_source(object_change, bucket_change, message):
    now = datetime(2026, 11, 17, tzinfo=timezone.utc)
    object_metadata = {
        "name": "atlas-prod-backup.sql",
        "bucket": "atlas-staging-510915-db-backups",
        "generation": "1750000000000000",
        "size": "12345",
        "crc32c": "AAAAAA==",
        "retentionExpirationTime": (now + timedelta(days=20)).isoformat(),
    }
    object_metadata.update(object_change)
    bucket_metadata = {"retentionPolicy": {"isLocked": True}}
    bucket_metadata.update(bucket_change)
    with pytest.raises(ValueError, match=message):
        validate_backup_metadata(
            backup_reference=BACKUP,
            object_metadata=object_metadata,
            bucket_metadata=bucket_metadata,
            now=now,
        )


@pytest.mark.parametrize(
    "document, expected",
    [
        ({"md5_hash": "md5", "crc32c_hash": "crc"}, "md5"),
        ({"md5Hash": "md5", "crc32c": "crc"}, "md5"),
        ({"retain_until_time": "2030-01-01T00:00:00Z"}, "2030-01-01T00:00:00Z"),
    ],
)
def test_provider_metadata_alias_picker_supports_snake_case_and_camel_case(document, expected):
    assert _pick(document, "md5_hash", "md5Hash", "crc32c_hash", "crc32c", "retain_until_time", "retainUntilTime") == expected


def test_backup_metadata_accepts_gcloud_snake_case_cli_output():
    now = datetime(2026, 11, 17, tzinfo=timezone.utc)
    validate_backup_metadata(
        backup_reference=BACKUP,
        object_metadata={
            "name": "atlas-prod-backup.sql",
            "bucket": "atlas-staging-510915-db-backups",
            "generation": "1750000000000000",
            "size": "12345",
            "crc32c_hash": "AAAAAA==",
            "retention_expiration_time": (now + timedelta(days=20)).isoformat(),
        },
        bucket_metadata={"retention_policy": {"is_locked": True}},
        now=now,
    )


def test_cloud_sql_import_object_suffix_matches_source_compression(tmp_path):
    plain_dump = tmp_path / "source.sql"
    gzip_dump = tmp_path / "source.sql.gz"
    plain_dump.write_bytes(b"-- plain SQL\\nSELECT 1;\\n")
    gzip_dump.write_bytes(gzip.compress(b"-- compressed SQL\\nSELECT 1;\\n"))

    assert _sql_import_suffix(plain_dump) == ".sql"
    assert _sql_import_suffix(gzip_dump) == ".sql.gz"


def test_recovery_preflight_excludes_generated_import_copies_even_when_no_source_backup_exists():
    generated_import_objects = [
        {
            "name": "restore-imports/" + ("a" * 64) + "-" + ("b" * 32) + ".sql",
            "generation": "1750000000000000",
            "size": "12345",
            "crc32c": "AAAAAA==",
        },
        {
            "name": "restore-imports/" + ("c" * 64) + "-" + ("d" * 32) + ".sql.gz",
            "generation": "1750000000000001",
            "size": "12345",
            "md5Hash": "AAAAAA==",
        },
    ]

    candidates = [item for item in generated_import_objects if is_source_backup_candidate(item)]

    assert candidates == []
    assert not any(is_source_backup_candidate(item) for item in generated_import_objects)
    assert is_source_backup_candidate({
        "name": "scheduled/postgres-backup.sql.gz",
        "generation": "1750000000000002",
        "size": "54321",
        "crc32c": "BBBBBB==",
    })


def test_operation_id_extraction_rejects_malformed_resource_names():
    assert _extract_operation_id({"name": "operation-123"}) == "operation-123"
    for document in ({}, {"name": "projects/prod/operations/123"}, {"name": "bad id"}):
        with pytest.raises(ValueError):
            _extract_operation_id(document)


def test_wait_for_operation_requires_successful_import_into_the_exact_database_and_object():
    operation = {
        "name": "operation-123",
        "operationType": "IMPORT",
        "status": "DONE",
        "importContext": {
            "database": "atlas-recovery-db",
            "uri": "gs://atlas-staging-510915-db-backups/atlas-prod-backup.sql",
        },
    }

    def fake_run(command, **kwargs):
        assert command[:4] == ["gcloud", "sql", "operations", "describe", "operation-123"][:4]
        return SimpleNamespace(returncode=0, stdout=json.dumps(operation), stderr="")

    result = _wait_for_operation(
        operation_id="operation-123",
        project_id="atlas-staging-510915",
        target_database="atlas-recovery-db",
        backup_object_uri="gs://atlas-staging-510915-db-backups/atlas-prod-backup.sql",
        timeout_seconds=60,
        poll_interval_seconds=1,
        run=fake_run,
    )
    assert result["status"] == "DONE"


def test_wait_for_operation_rejects_provider_error_and_wrong_target_database():
    def fake_run(document):
        def invoke(_command, **_kwargs):
            return SimpleNamespace(returncode=0, stdout=json.dumps(document), stderr="")
        return invoke

    base = {
        "operationType": "IMPORT",
        "status": "DONE",
        "importContext": {
            "database": "atlas-recovery-db",
            "uri": "gs://atlas-staging-510915-db-backups/atlas-prod-backup.sql",
        },
    }
    with pytest.raises(RuntimeError, match="provider errors"):
        _wait_for_operation(
            operation_id="operation-123",
            project_id="atlas-staging-510915",
            target_database="atlas-recovery-db",
            backup_object_uri="gs://atlas-staging-510915-db-backups/atlas-prod-backup.sql",
            timeout_seconds=60,
            poll_interval_seconds=1,
            run=fake_run({**base, "error": {"errors": [{"reason": "backendError"}]}}),
        )
    with pytest.raises(ValueError, match="different database"):
        _wait_for_operation(
            operation_id="operation-123",
            project_id="atlas-staging-510915",
            target_database="atlas-recovery-db",
            backup_object_uri="gs://atlas-staging-510915-db-backups/atlas-prod-backup.sql",
            timeout_seconds=60,
            poll_interval_seconds=1,
            run=fake_run({
                **base,
                "importContext": {
                    **base["importContext"],
                    "database": "postgres",
                },
            }),
        )



def test_pinned_generation_must_be_the_live_object_before_import():
    instant = datetime(2026, 11, 17, 12, tzinfo=timezone.utc)
    versions = {
        "items": [
            {
                "name": "atlas-prod-backup.sql",
                "generation": "1750000000000000",
                "timeCreated": "2026-11-16T00:00:00Z",
            },
            {
                "name": "atlas-prod-backup.sql",
                "generation": "1750000000000001",
                "timeCreated": "2026-11-17T11:00:00Z",
            },
        ]
    }
    assert _live_backup_generation(
        versions, object_name="atlas-prod-backup.sql", instant=instant
    ) == "1750000000000001"
    with pytest.raises(ValueError, match="no retained generation"):
        _live_backup_generation(
            {"items": []}, object_name="atlas-prod-backup.sql", instant=instant
        )


def test_cloud_sql_restore_workflow_executes_import_and_automatically_chains_audit():
    workflow = Path(".github/workflows/staging-cloud-sql-restore.yml").read_text(encoding="utf-8")
    audit_workflow = Path(".github/workflows/staging-restore-audit.yml").read_text(encoding="utf-8")
    executor = Path("scripts/execute_cloud_sql_restore.py").read_text(encoding="utf-8")
    preflight = Path(".github/workflows/cloud-sql-recovery-restore-preflight.yml").read_text(encoding="utf-8")
    discovery_preflight = Path(".github/workflows/cloud-sql-recovery-preflight.yml").read_text(encoding="utf-8")

    assert "workflow_dispatch:" in workflow
    assert 'confirm_restore_target' in workflow
    assert '== "RESTORE"' in workflow
    assert "GCP_STAGING_RESTORE_EXECUTOR_SERVICE_ACCOUNT" in workflow
    assert "scripts/execute_cloud_sql_restore.py" in workflow
    assert 'RESTORE_TARGET_KIND" == "cloud_sql"' in workflow
    assert "atlas_audit_reader" in workflow
    assert "inputs[source_backup_reference]" in workflow
    assert "import_object_reference" in workflow
    assert "[.]sql([.]gz)?" in workflow
    assert "[.]sql([.]gz)?" in audit_workflow
    assert "gh api --method POST" in workflow
    assert "staging-restore-audit.yml/dispatches" in workflow
    assert "gh run watch" in workflow
    assert "run-name: Cloud SQL restore audit (" in audit_workflow
    assert "SOURCE_BACKUP_REFERENCE" in audit_workflow
    assert "compare_cloud_sql_import_dumps.py" in audit_workflow
    assert "canonicalized conversion" in Path("scripts/compare_cloud_sql_import_dumps.py").read_text(encoding="utf-8")
    assert "prepare_cloud_sql_import_dump.py" in audit_workflow
    assert "Install pinned PostgreSQL 17 client for custom archive verification" in audit_workflow
    assert "Install pinned PostgreSQL 17 client for custom archive conversion" in workflow
    assert "postgresql-client-17" in Path("scripts/install_pinned_postgresql_client.sh").read_text(encoding="utf-8")
    assert "Verify a real PostgreSQL 17 custom archive conversion" in Path(".github/workflows/ci.yml").read_text(encoding="utf-8")
    assert "pg_dump --format=custom" in Path(".github/workflows/ci.yml").read_text(encoding="utf-8")
    assert "bash -n scripts/install_pinned_postgresql_client.sh" in Path(".github/workflows/ci.yml").read_text(encoding="utf-8")
    assert "RESTORE_TARGET_KIND" in audit_workflow
    assert '"gcloud", "sql", "import", "sql"' in executor
    assert '"--async"' in executor
    assert "verify_cloud_sql_import_dump.py" in executor
    assert "_live_backup_generation" in executor
    assert "verify_backup_binding(" in executor
    assert "verify_restore_operation(" in executor
    assert "def _pick(" in executor
    assert "--if-generation-match=0" in executor
    assert "import_object_reference" in executor
    assert "source_backup_sha256" in executor
    assert "import_sha256 != prepared_import_sha256" in executor
    assert "prepare_cloud_sql_import_dump.py" in executor
    assert "prepared_import_sha256" in executor
    assert "restore-imports/" in executor
    assert ".sql.gz" in executor
    assert "classify_backup_objects(" in preflight
    assert "ATLAS_PRODUCTION_CLOUD_SQL_DATABASE" in preflight
    assert "expected_source_database=source_database" in preflight
    assert "require_supported_postgresql_major(" in preflight
    assert "scripts.cloud_sql_recovery_inventory" in preflight
    assert "scripts/cloud_sql_recovery_discovery.py" in discovery_preflight
    assert '.lower().endswith((".sql", ".sql.gz", ".dump", ".backup", ".gz"))' not in preflight


def test_cloud_sql_restore_iam_bootstrap_is_separate_and_least_privilege():
    bootstrap = Path("scripts/bootstrap_cloud_sql_restore_iam.sh").read_text(encoding="utf-8")
    assert "atlas-staging-restore-executor" in bootstrap
    assert "atlas-staging-restore-audit" in bootstrap
    assert "cloudsql.instances.import" in bootstrap
    assert "cloudsql.instances.connect" in bootstrap
    assert "roles/storage.objectViewer" in bootstrap
    assert "roles/storage.objectCreator" in bootstrap
    assert "roles/secretmanager.secretAccessor" in bootstrap
    assert "roles/storage.objectCreator" in bootstrap
    assert "RECOVERY_INSTANCE_SA" in bootstrap
    assert "serviceAccountEmailAddress" in bootstrap
    assert "--condition=" in bootstrap
    assert "cloudsql.instances.import" in bootstrap
    assert "secretmanager.versions.access" not in bootstrap
    assert "refs/heads/main" in bootstrap
    assert "gcloud sql instances create" not in bootstrap
    assert "gcloud sql databases create" not in bootstrap
    assert "gcloud sql import" not in bootstrap

def test_recovery_preflight_accepts_retained_noncurrent_source_backup_generation():
    # GCS reports timeDeleted for an archived object generation. Its presence in
    # the versions listing means this exact generation remains retrievable.
    retained_generation = {
        "name": "scheduled/postgres-backup.sql.gz",
        "generation": "1750000000000003",
        "size": "54321",
        "crc32c": "CCCCCC==",
        "timeDeleted": "2026-11-17T10:00:00Z",
    }

    assert is_source_backup_candidate(retained_generation)
