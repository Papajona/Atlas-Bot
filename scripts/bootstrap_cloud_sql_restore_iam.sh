#!/usr/bin/env bash
set -euo pipefail
umask 077

# One-time, idempotent IAM bootstrap for the Cloud SQL recovery executor/auditor.
# Run from an identity authorized to manage IAM in atlas-staging-510915. This
# creates identities and bucket/secret IAM bindings only; it does not create,
# import into, or modify any Cloud SQL database.

PROJECT_ID="atlas-staging-510915"
PROJECT_NUMBER="350896810949"
REGION="europe-west1"
POOL_ID="atlas-bot-staging"
PROVIDER_ID="github-main"
EXECUTOR_ID="atlas-staging-restore-executor"
AUDIT_ID="atlas-staging-restore-audit"
BACKUP_BUCKET="atlas-staging-510915-db-backups"
AUDIT_DATABASE_SECRET="${ATLAS_STAGING_AUDIT_DATABASE_SECRET:-atlas-staging-audit-db-url}"

: "${ATLAS_STAGING_EVIDENCE_BUCKET:?Set ATLAS_STAGING_EVIDENCE_BUCKET to the existing restricted GCS evidence bucket}"
: "${ATLAS_STAGING_CLOUD_SQL_INSTANCE_CONNECTION_NAME:?Set the existing non-production recovery Cloud SQL instance connection name}"
: "${ATLAS_STAGING_CLOUD_SQL_DATABASE:?Set the existing recovery database name}"
: "${ATLAS_PRODUCTION_CLOUD_SQL_INSTANCE_CONNECTION_NAME:?Set the production Cloud SQL instance identity for the negative target check}"

[[ "$ATLAS_STAGING_CLOUD_SQL_INSTANCE_CONNECTION_NAME" =~ ^[a-z0-9-]+:[a-z0-9-]+:[a-z0-9-]+$ ]]
[[ "$ATLAS_PRODUCTION_CLOUD_SQL_INSTANCE_CONNECTION_NAME" =~ ^[a-z0-9-]+:[a-z0-9-]+:[a-z0-9-]+$ ]]
[[ "$ATLAS_STAGING_CLOUD_SQL_INSTANCE_CONNECTION_NAME" != "$ATLAS_PRODUCTION_CLOUD_SQL_INSTANCE_CONNECTION_NAME" ]] || {
  echo "BLOCKED: recovery and production Cloud SQL instances must be different" >&2
  exit 1
}
[[ "${ATLAS_STAGING_CLOUD_SQL_INSTANCE_CONNECTION_NAME%%:*}" == "$PROJECT_ID" ]] || {
  echo "BLOCKED: recovery Cloud SQL instance must be in the approved staging project" >&2
  exit 1
}
[[ "${ATLAS_STAGING_CLOUD_SQL_INSTANCE_CONNECTION_NAME#*:}" == "$REGION:"* ]] || {
  echo "BLOCKED: recovery Cloud SQL instance must be in the approved staging region" >&2
  exit 1
}
[[ "$ATLAS_STAGING_CLOUD_SQL_DATABASE" =~ ^[A-Za-z_][A-Za-z0-9_-]{0,62}$ ]]
[[ "$BACKUP_BUCKET" == "$PROJECT_ID-db-backups" ]]

gcloud config set project "$PROJECT_ID" >/dev/null
gcloud services enable sqladmin.googleapis.com iam.googleapis.com iamcredentials.googleapis.com storage.googleapis.com secretmanager.googleapis.com --project="$PROJECT_ID"

ACTUAL_PROJECT_NUMBER="$(gcloud projects describe "$PROJECT_ID" --format='value(projectNumber)')"
[[ "$ACTUAL_PROJECT_NUMBER" == "$PROJECT_NUMBER" ]] || {
  echo "BLOCKED: unexpected Google Cloud project number" >&2
  exit 1
}

PROVIDER_CONDITION="$(gcloud iam workload-identity-pools providers describe "$PROVIDER_ID" \
  --workload-identity-pool="$POOL_ID" --location=global --project="$PROJECT_ID" \
  --format='value(attributeCondition)')"
[[ "$PROVIDER_CONDITION" == *"assertion.repository == 'Papajona/Atlas-Bot'"* ]] || {
  echo "BLOCKED: GitHub workload identity provider is not restricted to Papajona/Atlas-Bot" >&2
  exit 1
}
[[ "$PROVIDER_CONDITION" == *"assertion.ref == 'refs/heads/main'"* ]] || {
  echo "BLOCKED: GitHub workload identity provider is not restricted to refs/heads/main" >&2
  exit 1
}

gcloud storage buckets describe "gs://$BACKUP_BUCKET" >/dev/null
gcloud storage buckets describe "gs://$ATLAS_STAGING_EVIDENCE_BUCKET" >/dev/null
if ! gcloud secrets describe "$AUDIT_DATABASE_SECRET" --project="$PROJECT_ID" >/dev/null 2>&1; then
  echo "BLOCKED: the dedicated restore audit database secret does not exist; no placeholder secret was created" >&2
  exit 1
fi

ensure_service_account() {
  local account_id="$1"
  local display_name="$2"
  local email="${account_id}@${PROJECT_ID}.iam.gserviceaccount.com"
  if ! gcloud iam service-accounts describe "$email" --project="$PROJECT_ID" >/dev/null 2>&1; then
    gcloud iam service-accounts create "$account_id" --project="$PROJECT_ID" --display-name="$display_name"
  fi
}

ensure_project_role() {
  local role_id="$1"
  local title="$2"
  local description="$3"
  local permissions="$4"
  if gcloud iam roles describe "$role_id" --project="$PROJECT_ID" >/dev/null 2>&1; then
    gcloud iam roles update "$role_id" --project="$PROJECT_ID" --title="$title" \
      --description="$description" --permissions="$permissions" --stage=GA
  else
    gcloud iam roles create "$role_id" --project="$PROJECT_ID" --title="$title" \
      --description="$description" --permissions="$permissions" --stage=GA
  fi
}

ensure_service_account "$EXECUTOR_ID" "Atlas isolated Cloud SQL restore executor"
ensure_service_account "$AUDIT_ID" "Atlas read-only Cloud SQL restore auditor"

EXECUTOR_EMAIL="${EXECUTOR_ID}@${PROJECT_ID}.iam.gserviceaccount.com"
AUDIT_EMAIL="${AUDIT_ID}@${PROJECT_ID}.iam.gserviceaccount.com"
EXECUTOR_ROLE="atlasCloudSqlRestoreExecutor"
AUDIT_ROLE="atlasCloudSqlRestoreAuditor"

# The executor can inspect/import only Cloud SQL resources and operations. GCS
# access is separately limited to the approved backup bucket below.
ensure_project_role "$EXECUTOR_ROLE" "Atlas Cloud SQL Restore Executor" \
  "Starts SQL imports and reads operations for the separately configured recovery instance." \
  "cloudsql.instances.get,cloudsql.instances.import,cloudsql.operations.get,cloudsql.databases.get,serviceusage.services.use"

# The auditor can connect through Cloud SQL Auth Proxy and read instance,
# database, operation and secret metadata. It cannot import, update or delete.
ensure_project_role "$AUDIT_ROLE" "Atlas Cloud SQL Restore Auditor" \
  "Read-only Cloud SQL restore verification and proxy access; no import or database writes." \
  "cloudsql.instances.get,cloudsql.instances.connect,cloudsql.operations.get,cloudsql.databases.get,serviceusage.services.use"

gcloud projects add-iam-policy-binding "$PROJECT_ID" \
  --member="serviceAccount:$EXECUTOR_EMAIL" \
  --role="projects/$PROJECT_ID/roles/$EXECUTOR_ROLE" --quiet >/dev/null
gcloud projects add-iam-policy-binding "$PROJECT_ID" \
  --member="serviceAccount:$AUDIT_EMAIL" \
  --role="projects/$PROJECT_ID/roles/$AUDIT_ROLE" --quiet >/dev/null

PRINCIPAL_SET="principalSet://iam.googleapis.com/projects/$PROJECT_NUMBER/locations/global/workloadIdentityPools/$POOL_ID/attribute.repository/Papajona/Atlas-Bot"
for email in "$EXECUTOR_EMAIL" "$AUDIT_EMAIL"; do
  gcloud iam service-accounts add-iam-policy-binding "$email" \
    --project="$PROJECT_ID" \
    --member="$PRINCIPAL_SET" \
    --role="roles/iam.workloadIdentityUser" >/dev/null
done

# The GitHub executor/auditor can read backup objects. None receives write access
# to the backup bucket.
for email in "$EXECUTOR_EMAIL" "$AUDIT_EMAIL"; do
  gcloud storage buckets add-iam-policy-binding "gs://$BACKUP_BUCKET" \
    --member="serviceAccount:$email" --role="roles/storage.objectViewer" --quiet >/dev/null
  gcloud storage buckets add-iam-policy-binding "gs://$BACKUP_BUCKET" \
    --member="serviceAccount:$email" --role="roles/storage.legacyBucketReader" --quiet >/dev/null
done

# Cloud SQL performs the server-side GCS read as the recovery instance's own
# service account, not the GitHub runner identity. Verify the configured
# instance is the recovery target before granting bucket access to its account.
RECOVERY_INSTANCE_ID="${ATLAS_STAGING_CLOUD_SQL_INSTANCE_CONNECTION_NAME##*:}"
RECOVERY_INSTANCE_JSON="$(gcloud sql instances describe "$RECOVERY_INSTANCE_ID" \
  --project="$PROJECT_ID" --format=json)"
RECOVERY_INSTANCE_SA="$(printf '%s' "$RECOVERY_INSTANCE_JSON" | python3 -c '
import json, sys
doc = json.load(sys.stdin)
expected = sys.argv[1]
observed = "%s:%s:%s" % (doc.get("project", ""), doc.get("region", ""), doc.get("name", ""))
if observed != expected:
    sys.exit("BLOCKED: recovery Cloud SQL instance identity does not match configuration")
email = doc.get("serviceAccountEmailAddress", "")
if not email:
    sys.exit("BLOCKED: recovery Cloud SQL instance has no service account")
print(email)
' "$ATLAS_STAGING_CLOUD_SQL_INSTANCE_CONNECTION_NAME")"
gcloud storage buckets add-iam-policy-binding "gs://$BACKUP_BUCKET" \
  --member="serviceAccount:$RECOVERY_INSTANCE_SA" \
  --role="roles/storage.objectViewer" --quiet >/dev/null

# Auditor is read-only on the backup source and can create audit evidence only
# in the dedicated restricted evidence bucket.
gcloud storage buckets add-iam-policy-binding "gs://$ATLAS_STAGING_EVIDENCE_BUCKET" \
  --member="serviceAccount:$AUDIT_EMAIL" \
  --role="roles/storage.objectCreator" --quiet >/dev/null
gcloud storage buckets add-iam-policy-binding "gs://$ATLAS_STAGING_EVIDENCE_BUCKET" \
  --member="serviceAccount:$AUDIT_EMAIL" \
  --role="roles/storage.legacyBucketReader" --quiet >/dev/null
gcloud secrets add-iam-policy-binding "$AUDIT_DATABASE_SECRET" \
  --project="$PROJECT_ID" --member="serviceAccount:$AUDIT_EMAIL" \
  --role="roles/secretmanager.secretAccessor" --quiet >/dev/null

echo "PASS: Cloud SQL restore executor and read-only auditor IAM are provisioned."
printf 'GCP_PROJECT_ID=%s\n' "$PROJECT_ID"
printf 'GCP_REGION=%s\n' "$REGION"
printf 'GCP_WORKLOAD_IDENTITY_PROVIDER=projects/%s/locations/global/workloadIdentityPools/%s/providers/%s\n' "$PROJECT_NUMBER" "$POOL_ID" "$PROVIDER_ID"
printf 'GCP_STAGING_RESTORE_EXECUTOR_SERVICE_ACCOUNT=%s\n' "$EXECUTOR_EMAIL"
printf 'GCP_STAGING_RESTORE_AUDIT_SERVICE_ACCOUNT=%s\n' "$AUDIT_EMAIL"
printf 'ATLAS_STAGING_RESTORE_TARGET_KIND=cloud_sql\n'
printf 'ATLAS_STAGING_CLOUD_SQL_INSTANCE_CONNECTION_NAME=%s\n' "$ATLAS_STAGING_CLOUD_SQL_INSTANCE_CONNECTION_NAME"
printf 'ATLAS_STAGING_CLOUD_SQL_DATABASE=%s\n' "$ATLAS_STAGING_CLOUD_SQL_DATABASE"
printf 'ATLAS_PRODUCTION_CLOUD_SQL_INSTANCE_CONNECTION_NAME=%s\n' "$ATLAS_PRODUCTION_CLOUD_SQL_INSTANCE_CONNECTION_NAME"
printf 'ATLAS_STAGING_AUDIT_DATABASE_SECRET=%s\n' "$AUDIT_DATABASE_SECRET"
printf 'ATLAS_STAGING_AUDIT_DB_ROLE=atlas_audit_reader\n'
printf 'ATLAS_STAGING_EVIDENCE_BUCKET=%s\n' "$ATLAS_STAGING_EVIDENCE_BUCKET"
