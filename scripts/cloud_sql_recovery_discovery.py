"""Read-only Cloud SQL, IAM and GCS discovery with fail-closed status reporting."""

from __future__ import annotations

import argparse
import json
import subprocess
from pathlib import Path
from typing import Any, Callable
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlencode
from urllib.request import Request, urlopen

PASS = "PASS"


def _decode_json(stdout: str, expected_type: type) -> tuple[Any, str]:
    try:
        value = json.loads(stdout or "null")
    except (TypeError, json.JSONDecodeError):
        return None, "GCLOUD_JSON_INVALID"
    if not isinstance(value, expected_type):
        return None, "GCLOUD_RESPONSE_SHAPE_INVALID"
    if expected_type is list and any(not isinstance(item, dict) for item in value):
        return None, "GCLOUD_RESPONSE_SHAPE_INVALID"
    return value, PASS


def run_gcloud_json(
    args: list[str],
    *,
    project: str,
    expected_type: type,
    runner: Callable[..., Any] = subprocess.run,
    timeout: int = 90,
) -> tuple[Any, str]:
    """Run one read-only gcloud query and validate both JSON syntax and shape."""
    command = ["gcloud", *args, "--project", project, "--format=json"]
    try:
        result = runner(
            command,
            text=True,
            capture_output=True,
            check=False,
            timeout=timeout,
        )
    except (OSError, subprocess.SubprocessError, TimeoutError):
        return None, "GCLOUD_QUERY_FAILED"
    if getattr(result, "returncode", 1) != 0:
        return None, "GCLOUD_QUERY_FAILED"
    return _decode_json(getattr(result, "stdout", ""), expected_type)


def get_access_token(
    *,
    runner: Callable[..., Any] = subprocess.run,
) -> tuple[str | None, str]:
    """Get an impersonated discovery identity token without logging its contents."""
    try:
        result = runner(
            ["gcloud", "auth", "print-access-token"],
            text=True,
            capture_output=True,
            check=False,
            timeout=30,
        )
    except (OSError, subprocess.SubprocessError, TimeoutError):
        return None, "GCLOUD_ACCESS_TOKEN_FAILED"
    token = getattr(result, "stdout", "").strip()
    if getattr(result, "returncode", 1) != 0 or not token:
        return None, "GCLOUD_ACCESS_TOKEN_FAILED"
    return token, PASS


def fetch_gcs_object_versions(
    *,
    bucket: str,
    access_token: str,
    opener: Callable[..., Any] = urlopen,
) -> tuple[list[dict[str, Any]], str]:
    """List every GCS object generation; any failed page invalidates completeness."""
    objects: list[dict[str, Any]] = []
    page_token: str | None = None
    try:
        while True:
            query = {
                "versions": "true",
                "maxResults": "1000",
                "fields": (
                    "items(name,generation,size,timeCreated,timeDeleted,md5Hash,crc32c,"
                    "retentionExpirationTime),nextPageToken"
                ),
            }
            if page_token:
                query["pageToken"] = page_token
            url = (
                "https://storage.googleapis.com/storage/v1/b/"
                + quote(bucket, safe="")
                + "/o?"
                + urlencode(query)
            )
            request = Request(url, headers={"Authorization": "Bearer " + access_token})
            with opener(request, timeout=30) as response:
                payload = json.loads(response.read().decode("utf-8"))

            if not isinstance(payload, dict):
                return objects, "GCS_RESPONSE_SHAPE_INVALID"
            page_items = payload.get("items", [])
            if not isinstance(page_items, list) or any(
                not isinstance(item, dict) for item in page_items
            ):
                return objects, "GCS_RESPONSE_SHAPE_INVALID"
            next_token = payload.get("nextPageToken")
            if next_token is not None and (
                not isinstance(next_token, str) or not next_token
            ):
                return objects, "GCS_RESPONSE_SHAPE_INVALID"

            objects.extend(page_items)
            if next_token is None:
                return objects, PASS
            page_token = next_token
    except (HTTPError, URLError, TimeoutError, OSError, UnicodeDecodeError, json.JSONDecodeError):
        return objects, "GCS_OBJECT_LIST_FAILED"


def _require_dict_list(value: Any) -> bool:
    return isinstance(value, list) and all(isinstance(item, dict) for item in value)


def discover_resources(
    *,
    project: str,
    region: str,
    bucket: str,
    runner: Callable[..., Any] = subprocess.run,
    opener: Callable[..., Any] = urlopen,
) -> dict[str, Any]:
    """Discover staging resources; no create, update, restore, or IAM write operations."""
    statuses: dict[str, str] = {}
    sql_inventory: list[dict[str, Any]] = []

    instances, instances_status = run_gcloud_json(
        ["sql", "instances", "list"],
        project=project,
        expected_type=list,
        runner=runner,
    )
    statuses["cloud_sql_instances"] = instances_status

    if instances_status == PASS:
        if not _require_dict_list(instances):
            statuses["cloud_sql_instances"] = "GCLOUD_RESPONSE_SHAPE_INVALID"
        else:
            for index, instance in enumerate(instances):
                name = instance.get("name")
                if not isinstance(name, str) or not name.strip():
                    statuses[f"cloud_sql_instances[{index}]"] = "GCLOUD_RESPONSE_SHAPE_INVALID"
                    continue

                databases, database_status = run_gcloud_json(
                    ["sql", "databases", "list", f"--instance={name}"],
                    project=project,
                    expected_type=list,
                    runner=runner,
                )
                backups, backup_status = run_gcloud_json(
                    ["sql", "backups", "list", f"--instance={name}"],
                    project=project,
                    expected_type=list,
                    runner=runner,
                )
                statuses[f"cloud_sql_instances.{name}.databases"] = database_status
                statuses[f"cloud_sql_instances.{name}.backups"] = backup_status

                settings_value = instance.get("settings", {})
                settings_status = PASS
                if not isinstance(settings_value, dict):
                    settings_status = "GCLOUD_RESPONSE_SHAPE_INVALID"
                    settings_value = {}
                statuses[f"cloud_sql_instances.{name}.settings"] = settings_status

                sql_inventory.append(
                    {
                        "name": name,
                        "connection_name": instance.get("connectionName"),
                        "region": instance.get("region"),
                        "state": instance.get("state"),
                        "database_version": instance.get("databaseVersion"),
                        "tier": settings_value.get("tier"),
                        "databases": databases if database_status == PASS else [],
                        "database_inventory_status": database_status,
                        "backups": backups if backup_status == PASS else [],
                        "backup_inventory_status": backup_status,
                    }
                )

    accounts, accounts_status = run_gcloud_json(
        ["iam", "service-accounts", "list"],
        project=project,
        expected_type=list,
        runner=runner,
    )
    statuses["service_accounts"] = accounts_status
    if accounts_status == PASS and not _require_dict_list(accounts):
        statuses["service_accounts"] = "GCLOUD_RESPONSE_SHAPE_INVALID"
        accounts = []
    elif accounts_status != PASS:
        accounts = []

    bucket_meta, bucket_status = run_gcloud_json(
        ["storage", "buckets", "describe", f"gs://{bucket}"],
        project=project,
        expected_type=dict,
        runner=runner,
    )
    statuses["backup_bucket"] = bucket_status

    objects: list[dict[str, Any]] = []
    if bucket_status != PASS:
        object_status = "NOT_RUN_BUCKET_METADATA_FAILED"
    else:
        access_token, token_status = get_access_token(runner=runner)
        statuses["gcp_access_token"] = token_status
        if token_status != PASS or access_token is None:
            object_status = "NOT_RUN_ACCESS_TOKEN_FAILED"
        else:
            objects, object_status = fetch_gcs_object_versions(
                bucket=bucket,
                access_token=access_token,
                opener=opener,
            )
    statuses["gcs_object_versions"] = object_status

    keywords = ("backup", "postgres", "database", "supabase", "dump", "atlas")
    suffixes = (".sql", ".sql.gz", ".dump", ".backup", ".gz", ".bak")
    candidates = []
    generated_restore_import_objects_excluded = 0
    for item in objects:
        object_name = str(item.get("name") or "")
        lower_name = object_name.lower()
        if lower_name.startswith("restore-imports/"):
            generated_restore_import_objects_excluded += 1
            continue
        if any(lower_name.endswith(suffix) for suffix in suffixes) or any(
            word in lower_name for word in keywords
        ):
            candidates.append(
                {
                    "name": object_name,
                    "generation": item.get("generation"),
                    "size_bytes": item.get("size"),
                    "created_at": item.get("timeCreated"),
                    "deleted_at": item.get("timeDeleted"),
                    "checksum_present": bool(item.get("md5Hash") or item.get("crc32c")),
                    "retention_expires_at": item.get("retentionExpirationTime"),
                }
            )

    atlas_accounts = [
        {"email": account.get("email"), "disabled": account.get("disabled")}
        for account in accounts
        if str(account.get("email", "")).startswith("atlas-")
    ]

    retention = {}
    retention_status = (
        PASS if bucket_status == PASS else "NOT_AVAILABLE_BUCKET_METADATA_FAILED"
    )
    if bucket_status == PASS and isinstance(bucket_meta, dict):
        retention_key = (
            "retentionPolicy"
            if "retentionPolicy" in bucket_meta
            else "retention_policy"
            if "retention_policy" in bucket_meta
            else None
        )
        if retention_key is None:
            retention_status = "BUCKET_RETENTION_POLICY_MISSING"
        else:
            retention_value = bucket_meta[retention_key]
            if not isinstance(retention_value, dict):
                retention_status = "GCLOUD_RESPONSE_SHAPE_INVALID"
            else:
                retention = retention_value
                retention_locked = retention.get(
                    "isLocked", retention.get("is_locked")
                ) is True
                retention_status = (
                    PASS if retention_locked else "BUCKET_RETENTION_NOT_LOCKED"
                )
    statuses["backup_bucket.retention_policy"] = retention_status

    passed = sum(status == PASS for status in statuses.values())
    failed = [name for name, status in statuses.items() if status != PASS]
    if not failed:
        discovery_status = "COMPLETE"
    elif passed:
        discovery_status = "PARTIAL"
    else:
        discovery_status = "FAILED"

    return {
        "discovery_status": discovery_status,
        "project": project,
        "region": region,
        "authenticated_identity": "IMPERSONATED_READ_ONLY_DISCOVERY_SERVICE_ACCOUNT",
        "statuses": statuses,
        "completed_queries": passed,
        "failed_or_unavailable_queries": len(failed),
        "failed_query_names": failed,
        "cloud_sql_instances": sql_inventory,
        "runnable_instances_in_expected_region": sum(
            1 for row in sql_inventory
            if row.get("region") == region and row.get("state") == "RUNNABLE"
        ),
        "atlas_service_accounts": atlas_accounts,
        "backup_bucket": bucket,
        "backup_bucket_status": bucket_status,
        "bucket_retention_policy_status": retention_status,
        "bucket_retention_policy_locked": (
            retention.get("isLocked", retention.get("is_locked")) is True
        ),
        "gcs_object_versions_listed": len(objects),
        "generated_restore_import_objects_excluded": generated_restore_import_objects_excluded,
        "database_backup_candidates": candidates[:200],
        "candidate_list_truncated": len(candidates) > 200,
        "note": (
            "Read-only inventory. No restore, database creation, IAM grant, migration, "
            "or production write was attempted."
        ),
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--project", required=True)
    parser.add_argument("--region", required=True)
    parser.add_argument("--bucket", required=True)
    parser.add_argument("--output", required=True)
    args = parser.parse_args(argv)

    report = discover_resources(project=args.project, region=args.region, bucket=args.bucket)
    output_path = Path(args.output)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(
        json.dumps(report, sort_keys=True, indent=2) + "\n",
        encoding="utf-8",
    )
    print(json.dumps(report, sort_keys=True))
    return 0 if report["discovery_status"] == "COMPLETE" else 1


if __name__ == "__main__":
    raise SystemExit(main())
