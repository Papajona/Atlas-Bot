#!/usr/bin/env bash
set -euo pipefail
umask 077

# Idempotently provision the dedicated read-only discovery identity only.
# This can be run without re-running the interactive database-secret bootstrap.
PROJECT_ID="atlas-staging-510915"
PROJECT_NUMBER="350896810949"
REGION="europe-west1"
POOL_ID="atlas-bot-staging"
PROVIDER_ID="github-main"
DISCOVERY_SA_ID="atlas-staging-discovery"
BACKUP_BUCKET="atlas-staging-510915-db-backups"

DISCOVERY_SA="${DISCOVERY_SA_ID}@${PROJECT_ID}.iam.gserviceaccount.com"
PRINCIPAL_SET="principalSet://iam.googleapis.com/projects/${PROJECT_NUMBER}/locations/global/workloadIdentityPools/${POOL_ID}/attribute.repository/Papajona/Atlas-Bot"

gcloud config set project "$PROJECT_ID" >/dev/null
ACTUAL_PROJECT_NUMBER="$(gcloud projects describe "$PROJECT_ID" --format='value(projectNumber)')"
if [[ "$ACTUAL_PROJECT_NUMBER" != "$PROJECT_NUMBER" ]]; then
  echo "BLOCKED: unexpected Google Cloud project number." >&2
  exit 1
fi

PROVIDER_CONDITION="$(gcloud iam workload-identity-pools providers describe "$PROVIDER_ID" \
  --workload-identity-pool="$POOL_ID" --location=global --project="$PROJECT_ID" \
  --format='value(attributeCondition)')"
PROVIDER_CONDITION="$(printf '%s' "$PROVIDER_CONDITION" | tr -s '[:space:]' ' ' | sed 's/^ //; s/ $//')"
EXPECTED_PROVIDER_CONDITION="assertion.repository == 'Papajona/Atlas-Bot' && assertion.ref == 'refs/heads/main'"
if [[ "$PROVIDER_CONDITION" != "$EXPECTED_PROVIDER_CONDITION" ]]; then
  echo "BLOCKED: GitHub Workload Identity condition must exactly restrict Papajona/Atlas-Bot to refs/heads/main." >&2
  exit 1
fi

gcloud services enable sqladmin.googleapis.com storage.googleapis.com \
  --project="$PROJECT_ID" >/dev/null

# Do not begin identity/IAM changes when the required inventory bucket is absent.
# The bucket is intentionally not created here: backup retention and ownership are
# separately approved infrastructure controls.
if ! gcloud storage buckets describe "gs://${BACKUP_BUCKET}" \
    --project="${PROJECT_ID}" --format=json >/dev/null 2>&1; then
  echo "BLOCKED: required backup bucket gs://${BACKUP_BUCKET} is not provisioned; no bucket IAM binding was applied." >&2
  exit 1
fi

if ! gcloud iam service-accounts describe "$DISCOVERY_SA" \
    --project="$PROJECT_ID" >/dev/null 2>&1; then
  gcloud iam service-accounts create "$DISCOVERY_SA_ID" \
    --project="$PROJECT_ID" \
    --display-name="Atlas read-only Cloud SQL and backup discovery"
fi

# Project-scoped viewer roles are read-only and limited to inventory APIs.
gcloud projects add-iam-policy-binding "$PROJECT_ID" \
  --member="serviceAccount:${DISCOVERY_SA}" \
  --role="roles/cloudsql.viewer" --quiet
gcloud projects add-iam-policy-binding "$PROJECT_ID" \
  --member="serviceAccount:${DISCOVERY_SA}" \
  --role="roles/iam.serviceAccountViewer" --quiet

# Bucket-scoped legacyBucketReader permits bucket metadata and object listing,
# but does not grant object-content reads, object writes, or bucket administration.
gcloud storage buckets add-iam-policy-binding "gs://$BACKUP_BUCKET" \
  --member="serviceAccount:${DISCOVERY_SA}" \
  --role="roles/storage.legacyBucketReader" --quiet

# Only the protected main-branch repository principal may impersonate this identity.
gcloud iam service-accounts add-iam-policy-binding "$DISCOVERY_SA" \
  --project="$PROJECT_ID" \
  --member="$PRINCIPAL_SET" \
  --role="roles/iam.workloadIdentityUser" --quiet

echo "PASS: dedicated Atlas discovery identity provisioned with read-only Cloud SQL, IAM-list, and GCS inventory permissions."
printf 'GCP_STAGING_DISCOVERY_SERVICE_ACCOUNT=%s\n' "$DISCOVERY_SA"
