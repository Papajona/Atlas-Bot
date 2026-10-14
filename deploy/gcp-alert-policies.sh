#!/usr/bin/env bash
set -euo pipefail

: "${GCP_PROJECT_ID:?Set GCP_PROJECT_ID to the intended staging/production project}"
: "${GCP_REGION:?Set GCP_REGION to the Cloud Run region}"
: "${CLOUD_RUN_SERVICE:?Set CLOUD_RUN_SERVICE to the exact Atlas Cloud Run service}"
: "${NOTIFICATION_CHANNEL_ID:?Set NOTIFICATION_CHANNEL_ID to a Monitoring notification-channel ID}"

[[ "$GCP_PROJECT_ID" =~ ^[a-z][a-z0-9-]{4,28}[a-z0-9]$ ]] || { echo "ERROR: invalid GCP_PROJECT_ID" >&2; exit 2; }
[[ "$GCP_REGION" =~ ^[a-z]+-[a-z]+[0-9]$ ]] || { echo "ERROR: invalid GCP_REGION" >&2; exit 2; }
[[ "$CLOUD_RUN_SERVICE" =~ ^[a-z][a-z0-9-]{0,48}$ ]] || { echo "ERROR: invalid Cloud Run service name" >&2; exit 2; }
[[ "$NOTIFICATION_CHANNEL_ID" =~ ^[a-zA-Z0-9_-]+$ ]] || { echo "ERROR: provide the notification channel ID, not an arbitrary resource path" >&2; exit 2; }

gcloud config set project "$GCP_PROJECT_ID" >/dev/null
WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT

CHANNEL="projects/$GCP_PROJECT_ID/notificationChannels/$NOTIFICATION_CHANNEL_ID"
if ! gcloud monitoring channels describe "$NOTIFICATION_CHANNEL_ID" --project="$GCP_PROJECT_ID" --format='value(name)' | grep -Fxq "$CHANNEL"; then
  echo "ERROR: notification channel does not exist in the configured GCP project." >&2
  exit 2
fi

# Verify metric labels/units before constructing filters; a misspelled label must not
# silently create an alert that can never fire.
for METRIC in \
  run.googleapis.com/request_count \
  run.googleapis.com/request_latencies \
  run.googleapis.com/container/cpu/utilizations \
  run.googleapis.com/container/memory/utilizations; do
  gcloud monitoring metrics-descriptors describe "$METRIC" --project="$GCP_PROJECT_ID" --format=json > "$WORK/atlas-metric-descriptor.json" || {
    echo "ERROR: Cloud Monitoring metric descriptor is unavailable: $METRIC" >&2
    exit 2
  }
done
gcloud monitoring metrics-descriptors describe run.googleapis.com/request_count --project="$GCP_PROJECT_ID" --format=json > "$WORK/atlas-request-count-descriptor.json"
gcloud monitoring metrics-descriptors describe run.googleapis.com/request_latencies --project="$GCP_PROJECT_ID" --format=json > "$WORK/atlas-request-latencies-descriptor.json"
python - <<'PY'
import json
import os
from pathlib import Path
request = json.loads((Path(os.environ["WORK"]) / "atlas-request-count-descriptor.json").read_text(encoding="utf-8"))
labels = {str(item.get("key", "")) for item in request.get("labels", [])}
if "response_code_class" not in labels:
    raise SystemExit("ERROR: request_count metric has no response_code_class label; revise the alert filter for this project's metric schema")
latency = json.loads((Path(os.environ["WORK"]) / "atlas-request-latencies-descriptor.json").read_text(encoding="utf-8"))
unit = str(latency.get("unit", "")).lower()
if not unit.startswith("ms"):
    raise SystemExit(f"ERROR: request latency unit is not milliseconds (observed unit={unit!r}); do not apply the 2000 ms threshold")
PY

export CHANNEL GCP_PROJECT_ID GCP_REGION CLOUD_RUN_SERVICE WORK

python - <<'PY'
import json
import os
from pathlib import Path

project = os.environ["GCP_PROJECT_ID"]
region = os.environ["GCP_REGION"]
service = os.environ["CLOUD_RUN_SERVICE"]
channel = os.environ["CHANNEL"]
out = Path(os.environ["WORK"])

base_resource = (
    'resource.type = "cloud_run_revision"'
    f' AND resource.labels.service_name = "{service}"'
    f' AND resource.labels.location = "{region}"'
)

def policy(display_name, metric_type, extra_filter, aligner, threshold, comparison, duration, unit_note):
    filt = f'{base_resource} AND metric.type = "{metric_type}"{extra_filter}'
    return {
        "displayName": display_name,
        "documentation": {"content": unit_note, "mimeType": "text/markdown"},
        "userLabels": {"managed_by": "atlas-audit-hardening", "service": service},
        "combiner": "OR",
        "notificationChannels": [channel],
        "enabled": True,
        "conditions": [{
            "displayName": display_name,
            "conditionThreshold": {
                "filter": filt,
                "comparison": comparison,
                "thresholdValue": threshold,
                "duration": duration,
                "aggregations": [{
                    "alignmentPeriod": "300s",
                    "perSeriesAligner": aligner,
                    "crossSeriesReducer": "REDUCE_SUM" if aligner in {"ALIGN_SUM", "ALIGN_RATE"} else "REDUCE_MAX",
                    "groupByFields": ["resource.labels.service_name", "resource.labels.location"],
                }],
                "trigger": {"count": 1},
            },
        }],
    }

policies = [
    policy(
        f"Atlas {service}: sustained 5xx responses",
        "run.googleapis.com/request_count",
        ' AND metric.labels.response_code_class = "5xx"',
        "ALIGN_SUM", 5, "COMPARISON_GT", "300s",
        "Triggers when at least six 5xx responses occur in an aligned five-minute period for this Cloud Run service.",
    ),
    policy(
        f"Atlas {service}: p95 request latency above 2 seconds",
        "run.googleapis.com/request_latencies",
        "",
        "ALIGN_PERCENTILE_95", 2000, "COMPARISON_GT", "300s",
        "Triggers when the p95 Cloud Run request latency remains above 2000 ms. Verify metric units in the project before relying on this policy.",
    ),
    policy(
        f"Atlas {service}: high CPU utilization",
        "run.googleapis.com/container/cpu/utilizations",
        "",
        "ALIGN_MEAN", 0.90, "COMPARISON_GT", "300s",
        "Triggers when mean container CPU utilization exceeds 90 percent.",
    ),
    policy(
        f"Atlas {service}: high memory utilization",
        "run.googleapis.com/container/memory/utilizations",
        "",
        "ALIGN_MEAN", 0.90, "COMPARISON_GT", "300s",
        "Triggers when mean container memory utilization exceeds 90 percent.",
    ),
]
for i, item in enumerate(policies, 1):
    (out / f"policy-{i}.json").write_text(json.dumps(item, indent=2) + "\n", encoding="utf-8")
PY

# Idempotently update policies managed by this script instead of creating duplicates.
POLICIES_JSON="$WORK/existing-policies.json"
gcloud monitoring policies list --project="$GCP_PROJECT_ID" --format=json > "$POLICIES_JSON"
for FILE in "$WORK"/policy-*.json; do
  DISPLAY_NAME="$(python -c 'import json,sys; print(json.load(open(sys.argv[1],encoding="utf-8"))["displayName"])' "$FILE")"
  POLICY_NAME="$(python - "$POLICIES_JSON" "$DISPLAY_NAME" <<'PY'
import json
import sys
policies = json.load(open(sys.argv[1], encoding="utf-8"))
matches = [p for p in policies if p.get("displayName") == sys.argv[2]]
if len(matches) > 1:
    raise SystemExit("ERROR: duplicate Monitoring policy display names; resolve them manually")
if not matches:
    print("")
else:
    policy = matches[0]
    if (policy.get("userLabels") or {}).get("managed_by") != "atlas-audit-hardening":
        raise SystemExit("ERROR: existing policy has this display name but is not managed by this script; rename or review it manually")
    print(policy.get("name", ""))
PY
)"
  if [[ -n "$POLICY_NAME" ]]; then
    ID="${POLICY_NAME##*/}"
    gcloud monitoring policies update "$ID" --project="$GCP_PROJECT_ID" --policy-from-file="$FILE"
  else
    gcloud monitoring policies create --project="$GCP_PROJECT_ID" --policy-from-file="$FILE"
  fi
done

echo "PASS: requested Atlas Cloud Monitoring policies were created or updated."
echo "REQUIRED FOLLOW-UP: inspect each policy in Cloud Monitoring and confirm a test notification was delivered to the configured channel."
