from __future__ import annotations

import json
import subprocess
from pathlib import Path
from types import SimpleNamespace
from urllib.error import URLError

import pytest

from scripts import cloud_sql_recovery_discovery as discovery
from scripts.cloud_sql_recovery_discovery import (
    _decode_json,
    discover_resources,
    fetch_gcs_object_versions,
    run_gcloud_json,
)


PROJECT = "atlas-staging-510915"
REGION = "europe-west1"
BUCKET = "atlas-staging-510915-db-backups"


def response(payload):
    return SimpleNamespace(returncode=0, stdout=json.dumps(payload), stderr="")


def make_response(payload):
    class FakeResponse:
        def __init__(self, value):
            self.value = value

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def read(self):
            return (
                self.value.encode("utf-8")
                if isinstance(self.value, str)
                else json.dumps(self.value).encode("utf-8")
            )

    return FakeResponse(payload)


def test_gcloud_permission_denied_is_a_query_failure_and_does_not_leak_stderr():
    def runner(*_args, **_kwargs):
        return SimpleNamespace(
            returncode=1,
            stdout="",
            stderr="PERMISSION_DENIED: private resource details",
        )

    data, status = run_gcloud_json(
        ["sql", "instances", "list"],
        project=PROJECT,
        expected_type=list,
        runner=runner,
    )
    assert data is None
    assert status == "GCLOUD_QUERY_FAILED"


def test_gcloud_invalid_json_is_not_reported_as_an_empty_inventory():
    def runner(*_args, **_kwargs):
        return SimpleNamespace(returncode=0, stdout="{not-json", stderr="")

    data, status = run_gcloud_json(
        ["sql", "instances", "list"],
        project=PROJECT,
        expected_type=list,
        runner=runner,
    )
    assert data is None
    assert status == "GCLOUD_JSON_INVALID"


@pytest.mark.parametrize("payload", [None, {}, "not-a-list", [1, "bad-entry"]])
def test_gcloud_wrong_json_shape_is_rejected(payload):
    data, status = _decode_json(json.dumps(payload), list)
    assert data is None
    assert status == "GCLOUD_RESPONSE_SHAPE_INVALID"


def test_gcloud_command_timeout_is_reported_as_failure():
    def runner(*_args, **_kwargs):
        raise subprocess.TimeoutExpired(cmd="gcloud", timeout=90)

    data, status = run_gcloud_json(
        ["sql", "instances", "list"],
        project=PROJECT,
        expected_type=list,
        runner=runner,
    )
    assert data is None
    assert status == "GCLOUD_QUERY_FAILED"


def test_gcs_malformed_json_is_not_reported_as_an_empty_success():
    def opener(*_args, **_kwargs):
        return make_response("{malformed")

    objects, status = fetch_gcs_object_versions(
        bucket=BUCKET,
        access_token="test-token",
        opener=opener,
    )
    assert objects == []
    assert status == "GCS_OBJECT_LIST_FAILED"


def test_gcs_wrong_page_shape_is_rejected():
    def opener(*_args, **_kwargs):
        return make_response({"items": {"not": "a-list"}})

    objects, status = fetch_gcs_object_versions(
        bucket=BUCKET,
        access_token="test-token",
        opener=opener,
    )
    assert objects == []
    assert status == "GCS_RESPONSE_SHAPE_INVALID"


def test_gcs_pagination_failure_preserves_partial_rows_but_marks_inventory_failed():
    calls = 0

    def opener(*_args, **_kwargs):
        nonlocal calls
        calls += 1
        if calls == 1:
            return make_response(
                {
                    "items": [{"name": "backup.sql.gz", "generation": "123"}],
                    "nextPageToken": "next-page",
                }
            )
        raise URLError("permission denied")

    objects, status = fetch_gcs_object_versions(
        bucket=BUCKET,
        access_token="test-token",
        opener=opener,
    )
    assert len(objects) == 1
    assert status == "GCS_OBJECT_LIST_FAILED"


def test_any_failed_top_level_inventory_query_makes_report_partial():
    def runner(command, **_kwargs):
        if command[1:3] == ["sql", "instances"]:
            return response([])
        if command[1:3] == ["iam", "service-accounts"]:
            return SimpleNamespace(returncode=1, stdout="", stderr="PERMISSION_DENIED")
        if command[1:4] == ["storage", "buckets", "describe"]:
            return response({"name": BUCKET, "retentionPolicy": {"isLocked": True}})
        if command[1:3] == ["auth", "print-access-token"]:
            return SimpleNamespace(returncode=0, stdout="token", stderr="")
        raise AssertionError(command)

    def opener(*_args, **_kwargs):
        return make_response({"items": []})

    report = discover_resources(
        project=PROJECT,
        region=REGION,
        bucket=BUCKET,
        runner=runner,
        opener=opener,
    )
    assert report["discovery_status"] == "PARTIAL"
    assert report["statuses"]["service_accounts"] == "GCLOUD_QUERY_FAILED"
    assert "service_accounts" in report["failed_query_names"]


def test_per_instance_database_failure_prevents_complete_status():
    def runner(command, **_kwargs):
        if command[1:3] == ["sql", "instances"]:
            return response(
                [
                    {
                        "name": "atlas-recovery",
                        "region": REGION,
                        "state": "RUNNABLE",
                        "settings": {},
                    }
                ]
            )
        if command[1:3] == ["sql", "databases"]:
            return SimpleNamespace(returncode=1, stdout="", stderr="PERMISSION_DENIED")
        if command[1:3] == ["sql", "backups"]:
            return response([])
        if command[1:3] == ["iam", "service-accounts"]:
            return response([])
        if command[1:4] == ["storage", "buckets", "describe"]:
            return response({"name": BUCKET, "retentionPolicy": {"isLocked": True}})
        if command[1:3] == ["auth", "print-access-token"]:
            return SimpleNamespace(returncode=0, stdout="token", stderr="")
        raise AssertionError(command)

    def opener(*_args, **_kwargs):
        return make_response({"items": []})

    report = discover_resources(
        project=PROJECT,
        region=REGION,
        bucket=BUCKET,
        runner=runner,
        opener=opener,
    )
    assert report["discovery_status"] == "PARTIAL"
    assert report["statuses"]["cloud_sql_instances.atlas-recovery.databases"] == "GCLOUD_QUERY_FAILED"


def test_all_successful_queries_produce_complete_status():
    def runner(command, **_kwargs):
        if command[1:3] == ["sql", "instances"]:
            return response([])
        if command[1:3] == ["iam", "service-accounts"]:
            return response([])
        if command[1:4] == ["storage", "buckets", "describe"]:
            return response({"name": BUCKET, "retentionPolicy": {"isLocked": True}})
        if command[1:3] == ["auth", "print-access-token"]:
            return SimpleNamespace(returncode=0, stdout="token", stderr="")
        raise AssertionError(command)

    def opener(*_args, **_kwargs):
        return make_response({"items": []})

    report = discover_resources(
        project=PROJECT,
        region=REGION,
        bucket=BUCKET,
        runner=runner,
        opener=opener,
    )
    assert report["discovery_status"] == "COMPLETE"
    assert report["failed_query_names"] == []


def test_bucket_failure_skips_gcs_and_never_claims_complete():
    def runner(command, **_kwargs):
        if command[1:3] == ["sql", "instances"]:
            return response([])
        if command[1:3] == ["iam", "service-accounts"]:
            return response([])
        if command[1:4] == ["storage", "buckets", "describe"]:
            return SimpleNamespace(returncode=1, stdout="", stderr="PERMISSION_DENIED")
        raise AssertionError(command)

    report = discover_resources(
        project=PROJECT,
        region=REGION,
        bucket=BUCKET,
        runner=runner,
        opener=lambda *_args, **_kwargs: pytest.fail("GCS must not run after bucket metadata failure"),
    )
    assert report["discovery_status"] == "PARTIAL"
    assert report["statuses"]["gcs_object_versions"] == "NOT_RUN_BUCKET_METADATA_FAILED"


def test_main_writes_partial_artifact_then_returns_nonzero(tmp_path, monkeypatch, capsys):
    report = {
        "discovery_status": "PARTIAL",
        "statuses": {"service_accounts": "GCLOUD_QUERY_FAILED"},
        "failed_query_names": ["service_accounts"],
    }
    monkeypatch.setattr(discovery, "discover_resources", lambda **_kwargs: report)
    output = tmp_path / "recovery-resource-discovery.json"
    exit_code = discovery.main(
        [
            "--project", PROJECT,
            "--region", REGION,
            "--bucket", BUCKET,
            "--output", str(output),
        ]
    )
    assert exit_code == 1
    assert json.loads(output.read_text(encoding="utf-8")) == report
    assert '"discovery_status": "PARTIAL"' in capsys.readouterr().out


@pytest.mark.parametrize("malformed_field", ["settings", "retentionPolicy"])
def test_malformed_nested_metadata_writes_partial_artifact(
    malformed_field, tmp_path, monkeypatch, capsys
):
    def runner(command, **_kwargs):
        if command[1:3] == ["sql", "instances"]:
            settings = "not-an-object" if malformed_field == "settings" else {}
            return response(
                [
                    {
                        "name": "atlas-recovery",
                        "region": REGION,
                        "state": "RUNNABLE",
                        "settings": settings,
                    }
                ]
            )
        if command[1:3] == ["sql", "databases"]:
            return response([{"name": "postgres"}])
        if command[1:3] == ["sql", "backups"]:
            return response([])
        if command[1:3] == ["iam", "service-accounts"]:
            return response([])
        if command[1:4] == ["storage", "buckets", "describe"]:
            retention = (
                "not-an-object"
                if malformed_field == "retentionPolicy"
                else {"isLocked": True}
            )
            return response({"name": BUCKET, "retentionPolicy": retention})
        if command[1:3] == ["auth", "print-access-token"]:
            return SimpleNamespace(returncode=0, stdout="token", stderr="")
        raise AssertionError(command)

    def opener(*_args, **_kwargs):
        return make_response({"items": []})

    original_discover = discovery.discover_resources

    def discover_with_fakes(**kwargs):
        return original_discover(**kwargs, runner=runner, opener=opener)

    monkeypatch.setattr(discovery, "discover_resources", discover_with_fakes)
    output = tmp_path / "recovery-resource-discovery.json"
    exit_code = discovery.main(
        [
            "--project", PROJECT,
            "--region", REGION,
            "--bucket", BUCKET,
            "--output", str(output),
        ]
    )

    report = json.loads(output.read_text(encoding="utf-8"))
    assert exit_code == 1
    assert report["discovery_status"] == "PARTIAL"
    assert report["failed_or_unavailable_queries"] >= 1
    if malformed_field == "settings":
        assert report["statuses"]["cloud_sql_instances.atlas-recovery.settings"] == (
            "GCLOUD_RESPONSE_SHAPE_INVALID"
        )
        assert report["cloud_sql_instances"][0]["name"] == "atlas-recovery"
    else:
        assert report["statuses"]["backup_bucket.retention_policy"] == (
            "GCLOUD_RESPONSE_SHAPE_INVALID"
        )
    assert '"discovery_status": "PARTIAL"' in capsys.readouterr().out


def test_discovery_bootstrap_stops_before_iam_when_backup_bucket_is_missing(tmp_path):
    import os

    bootstrap = (
        Path(__file__).resolve().parents[1]
        / "scripts"
        / "bootstrap_atlas_staging_discovery_identity.sh"
    )
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    calls_file = tmp_path / "gcloud-calls.jsonl"
    fake_gcloud = fake_bin / "gcloud"
    fake_gcloud.write_text(
        """#!/usr/bin/env python3
import json
import os
import sys

args = sys.argv[1:]
with open(os.environ["GCLOUD_CALL_LOG"], "a", encoding="utf-8") as handle:
    handle.write(json.dumps(args) + "\n")

if args[:2] == ["projects", "describe"]:
    print("350896810949")
elif args[:4] == ["iam", "workload-identity-pools", "providers", "describe"]:
    print(
        "assertion.repository == 'Papajona/Atlas-Bot' && "
        "assertion.ref == 'refs/heads/main'"
    )
elif args[:3] == ["storage", "buckets", "describe"]:
    print("bucket not found", file=sys.stderr)
    raise SystemExit(3)
""",
        encoding="utf-8",
    )
    fake_gcloud.chmod(0o755)
    env = os.environ.copy()
    env["PATH"] = str(fake_bin) + os.pathsep + env.get("PATH", "")
    env["GCLOUD_CALL_LOG"] = str(calls_file)

    result = subprocess.run(
        ["bash", str(bootstrap)],
        check=False,
        capture_output=True,
        text=True,
        env=env,
    )

    assert result.returncode != 0
    assert "required backup bucket" in result.stderr
    calls = [json.loads(line) for line in calls_file.read_text(encoding="utf-8").splitlines()]
    assert any(call[:3] == ["storage", "buckets", "describe"] for call in calls)
    assert not any("add-iam-policy-binding" in call for call in calls)
    assert not any(call[:3] == ["iam", "service-accounts", "create"] for call in calls)


def test_all_failed_inventory_queries_report_failed_without_synthetic_pass(
    tmp_path, monkeypatch, capsys
):
    def runner(*_args, **_kwargs):
        return SimpleNamespace(
            returncode=1,
            stdout="",
            stderr="PERMISSION_DENIED",
        )

    original_discover = discovery.discover_resources

    def discover_with_failed_queries(**kwargs):
        return original_discover(
            **kwargs,
            runner=runner,
            opener=lambda *_args, **_kwargs: pytest.fail(
                "GCS listing must not run after bucket metadata lookup fails"
            ),
        )

    monkeypatch.setattr(discovery, "discover_resources", discover_with_failed_queries)
    output = tmp_path / "recovery-resource-discovery.json"
    exit_code = discovery.main(
        [
            "--project", PROJECT,
            "--region", REGION,
            "--bucket", BUCKET,
            "--output", str(output),
        ]
    )

    report = json.loads(output.read_text(encoding="utf-8"))
    assert exit_code == 1
    assert report["discovery_status"] == "FAILED"
    assert all(status != "PASS" for status in report["statuses"].values())
    assert report["statuses"]["backup_bucket.retention_policy"] == (
        "NOT_AVAILABLE_BUCKET_METADATA_FAILED"
    )
    assert report["failed_or_unavailable_queries"] == len(report["statuses"])
    assert '"discovery_status": "FAILED"' in capsys.readouterr().out
