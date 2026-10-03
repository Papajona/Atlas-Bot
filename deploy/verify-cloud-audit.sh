#!/usr/bin/env bash
set -euo pipefail
: "${PROJECT_ID:?Set PROJECT_ID}"
BUCKET_ID="${BUCKET_ID:-atlas-security-audit}"
LOCATION="${LOCATION:-global}"
SINK_NAME="${SINK_NAME:-atlas-security-audit-sink}"
MIN_RETENTION_DAYS="${MIN_RETENTION_DAYS:-3650}"

echo "== Bucket =="
gcloud logging buckets describe "$BUCKET_ID" --project="$PROJECT_ID" --location="$LOCATION"
retention="$(gcloud logging buckets describe "$BUCKET_ID" --project="$PROJECT_ID" --location="$LOCATION" --format='value(retentionDays)')"
if [[ -z "$retention" || "$retention" -lt "$MIN_RETENTION_DAYS" ]]; then
  echo "ERROR: audit bucket retention ${retention:-unset} is below required ${MIN_RETENTION_DAYS} days." >&2
  exit 1
fi

echo "== Sink =="
sink_destination="$(gcloud logging sinks describe "$SINK_NAME" --project="$PROJECT_ID" --format='value(destination)')"
expected="logging.googleapis.com/projects/${PROJECT_ID}/locations/${LOCATION}/buckets/${BUCKET_ID}"
if [[ "$sink_destination" != "$expected" ]]; then
  echo "ERROR: audit sink destination mismatch: $sink_destination" >&2
  exit 1
fi
gcloud logging sinks describe "$SINK_NAME" --project="$PROJECT_ID"

echo "== Recent audit records (sample) =="
gcloud logging read 'jsonPayload.atlas_security_audit=true' --project="$PROJECT_ID" --limit=5 --format=json || true
echo
echo "Retention is verified. Bucket locking remains an explicit irreversible operator action after IAM/access review."
