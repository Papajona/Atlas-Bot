from datetime import datetime, timezone

import pytest

from scripts.cloud_sql_recovery_inventory import (
    classify_backup_objects,
    require_supported_postgresql_major,
)


NOW = datetime(2026, 11, 17, 12, 0, tzinfo=timezone.utc)
SOURCE_INSTANCE = "atlas-prod:europe-west1:atlas-production"
SOURCE_DATABASE = "postgres"


def backup_object(**overrides):
    item = {
        "name": "exports/atlas-production-postgres.sql.gz",
        "generation": "1720000000000000",
        "size": "4096",
        "timeCreated": "2026-11-17T10:00:00Z",
        "md5Hash": "valid-checksum",
        "retentionExpirationTime": "2027-11-17T10:00:00Z",
        "metadata": {
            "atlas_source_instance_connection_name": SOURCE_INSTANCE,
            "atlas_source_database": SOURCE_DATABASE,
        },
    }
    item.update(overrides)
    return item


@pytest.mark.parametrize(
    ("database_version", "expected_major"),
    [
        ("POSTGRES_14", 14),
        ("POSTGRES_15", 15),
        ("POSTGRES_16", 16),
        ("POSTGRES_17", 17),
        ("POSTGRES_18", 18),
        ("POSTGRES_17_2", 17),
    ],
)
def test_supported_postgresql_versions_are_accepted(database_version, expected_major):
    assert require_supported_postgresql_major(database_version) == expected_major


@pytest.mark.parametrize(
    "database_version",
    [None, "", "MYSQL_8_0", "SQLSERVER_2019_STANDARD", "POSTGRES_13", "POSTGRES_99"],
)
def test_non_postgresql_and_unsupported_versions_are_rejected(database_version):
    with pytest.raises(ValueError):
        require_supported_postgresql_major(database_version)


def test_live_retained_checksummed_source_associated_backup_is_qualified():
    qualified, unverified = classify_backup_objects(
        [backup_object()],
        expected_source_instance=SOURCE_INSTANCE,
        expected_source_database=SOURCE_DATABASE,
        now=NOW,
    )
    assert len(qualified) == 1
    assert qualified[0]["generation"] == "1720000000000000"
    assert qualified[0]["source_association"] == "matched_object_metadata"
    assert unverified == []


@pytest.mark.parametrize(
    ("overrides", "expected_reason"),
    [
        ({"timeDeleted": "2026-11-17T11:00:00Z"}, "not_a_live_object_generation"),
        ({"softDeleteTime": "2026-11-17T11:00:00Z"}, "not_a_live_object_generation"),
        ({"size": "0"}, "empty_or_invalid_size"),
        ({"size": None}, "empty_or_invalid_size"),
        ({"md5Hash": None}, "missing_checksum"),
        ({"generation": None}, "missing_or_invalid_generation"),
        ({"retentionExpirationTime": "2026-11-16T10:00:00Z"}, "retention_expired"),
        ({"retentionExpirationTime": None}, "missing_or_invalid_retention_expiration"),
        (
            {
                "metadata": {
                    "atlas_source_instance_connection_name": "other:europe-west1:unrelated",
                    "atlas_source_database": SOURCE_DATABASE,
                }
            },
            "source_instance_metadata_mismatch",
        ),
        (
            {
                "metadata": {
                    "atlas_source_instance_connection_name": SOURCE_INSTANCE,
                    "atlas_source_database": "unrelated",
                }
            },
            "source_database_metadata_mismatch",
        ),
        ({"metadata": {}}, "source_instance_metadata_mismatch"),
    ],
)
def test_unusable_or_unassociated_objects_never_qualify(overrides, expected_reason):
    qualified, unverified = classify_backup_objects(
        [backup_object(**overrides)],
        expected_source_instance=SOURCE_INSTANCE,
        expected_source_database=SOURCE_DATABASE,
        now=NOW,
    )
    assert qualified == []
    assert len(unverified) == 1
    assert expected_reason in unverified[0]["reasons"]


def test_backup_like_filename_without_metadata_is_unverified_not_qualified():
    qualified, unverified = classify_backup_objects(
        [{"name": "looks-like-a-backup.sql.gz"}],
        expected_source_instance=SOURCE_INSTANCE,
        expected_source_database=SOURCE_DATABASE,
        now=NOW,
    )
    assert qualified == []
    assert unverified[0]["source_association"] == "unverified"


def test_non_backup_objects_are_not_counted_as_candidates():
    qualified, unverified = classify_backup_objects(
        [{"name": "logs/restore.log", "size": "99"}],
        expected_source_instance=SOURCE_INSTANCE,
        expected_source_database=SOURCE_DATABASE,
        now=NOW,
    )
    assert qualified == []
    assert unverified == []


def test_source_identity_is_required():
    with pytest.raises(ValueError, match="must both be configured"):
        classify_backup_objects(
            [backup_object()],
            expected_source_instance="",
            expected_source_database=SOURCE_DATABASE,
            now=NOW,
        )
