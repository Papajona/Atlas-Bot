#!/usr/bin/env bash
set -euo pipefail
umask 077

# One-time operator bootstrap for the paper-only staging service.
# Run ONLY after both DB roles/schema have been provisioned and their passwords saved
# securely. Passwords and the Supabase API key are read without echo and sent to
# Secret Manager through stdin; they are never placed in command arguments or printed.

PROJECT_ID="atlas-staging-510915"
PROJECT_NUMBER="350896810949"
REGION="europe-west1"
POOL_ID="atlas-bot-staging"
PROVIDER_ID="github-main"

DEPLOY_SA_ID="atlas-staging-deploy"
PREFLIGHT_SA_ID="atlas-staging-preflight"
RUNTIME_SA_ID="atlas-staging-runtime"
ARTIFACT_REPOSITORY="atlas-staging"
CLOUD_RUN_SERVICE="atlas-paper-staging"

SUPABASE_PROJECT_REF="wauaelzyugjuepmrewaw"
SUPABASE_DB_HOST="aws-0-eu-west-1.pooler.supabase.com"
PRIMARY_SECRET="atlas-staging-primary-db-url"
PAPER_SECRET="atlas-staging-customer-paper-db-url"
ANON_KEY_SECRET="atlas-staging-supabase-anon-key"

gcloud config set project "$PROJECT_ID" >/dev/null
gcloud services enable \
  artifactregistry.googleapis.com \
  iam.googleapis.com \
  iamcredentials.googleapis.com \
  run.googleapis.com \
  secretmanager.googleapis.com \
  sts.googleapis.com \
  --project="$PROJECT_ID"

ACTUAL_PROJECT_NUMBER="$(gcloud projects describe "$PROJECT_ID" --format='value(projectNumber)')"
if [[ "$ACTUAL_PROJECT_NUMBER" != "$PROJECT_NUMBER" ]]; then
  echo "BLOCKED: unexpected Google Cloud project number."
  exit 1
fi

PROVIDER_CONDITION="$(gcloud iam workload-identity-pools providers describe "$PROVIDER_ID" \
  --workload-identity-pool="$POOL_ID" --location=global --project="$PROJECT_ID" \
  --format='value(attributeCondition)')"
if [[ "$PROVIDER_CONDITION" != *"assertion.repository == 'Papajona/Atlas-Bot'"* ]] ||
   [[ "$PROVIDER_CONDITION" != *"assertion.ref == 'refs/heads/main'"* ]]; then
  echo "BLOCKED: expected GitHub provider is missing or is not restricted to Papajona/Atlas-Bot on refs/heads/main."
  exit 1
fi

ensure_service_account() {
  local account_id="$1"
  local display_name="$2"
  local email="${account_id}@${PROJECT_ID}.iam.gserviceaccount.com"
  if ! gcloud iam service-accounts describe "$email" --project="$PROJECT_ID" >/dev/null 2>&1; then
    gcloud iam service-accounts create "$account_id" \
      --project="$PROJECT_ID" --display-name="$display_name"
  fi
}

ensure_service_account "$DEPLOY_SA_ID" "Atlas paper staging GitHub deploy"
ensure_service_account "$PREFLIGHT_SA_ID" "Atlas staging identity preflight"
ensure_service_account "$RUNTIME_SA_ID" "Atlas paper-only Cloud Run runtime"

DEPLOY_SA="${DEPLOY_SA_ID}@${PROJECT_ID}.iam.gserviceaccount.com"
PREFLIGHT_SA="${PREFLIGHT_SA_ID}@${PROJECT_ID}.iam.gserviceaccount.com"
RUNTIME_SA="${RUNTIME_SA_ID}@${PROJECT_ID}.iam.gserviceaccount.com"

if ! gcloud artifacts repositories describe "$ARTIFACT_REPOSITORY" \
    --location="$REGION" --project="$PROJECT_ID" >/dev/null 2>&1; then
  gcloud artifacts repositories create "$ARTIFACT_REPOSITORY" \
    --repository-format=docker --location="$REGION" --project="$PROJECT_ID" \
    --description="Atlas paper-only staging container images"
fi

# The deploy identity can deploy Cloud Run services in the staging project and write
# only to the single staging Artifact Registry repository.
gcloud projects add-iam-policy-binding "$PROJECT_ID" \
  --member="serviceAccount:${DEPLOY_SA}" --role="roles/run.admin" --quiet
gcloud artifacts repositories add-iam-policy-binding "$ARTIFACT_REPOSITORY" \
  --location="$REGION" --project="$PROJECT_ID" \
  --member="serviceAccount:${DEPLOY_SA}" --role="roles/artifactregistry.writer" --quiet
gcloud iam service-accounts add-iam-policy-binding "$RUNTIME_SA" \
  --project="$PROJECT_ID" --member="serviceAccount:${DEPLOY_SA}" \
  --role="roles/iam.serviceAccountUser"

PRINCIPAL_SET="principalSet://iam.googleapis.com/projects/${PROJECT_NUMBER}/locations/global/workloadIdentityPools/${POOL_ID}/attribute.repository/Papajona/Atlas-Bot"
gcloud iam service-accounts add-iam-policy-binding "$DEPLOY_SA" \
  --project="$PROJECT_ID" --member="$PRINCIPAL_SET" \
  --role="roles/iam.workloadIdentityUser"
gcloud iam service-accounts add-iam-policy-binding "$PREFLIGHT_SA" \
  --project="$PROJECT_ID" --member="$PRINCIPAL_SET" \
  --role="roles/iam.workloadIdentityUser"
gcloud projects add-iam-policy-binding "$PROJECT_ID" \
  --member="serviceAccount:${PREFLIGHT_SA}" --role="roles/run.viewer" --quiet

read -r -s -p "Enter the atlas_paper_reader database password (hidden): " PRIMARY_PASSWORD
printf '\n'
read -r -s -p "Enter the atlas_customer_paper database password (hidden): " PAPER_PASSWORD
printf '\n'
read -r -s -p "Enter the Supabase anon/publishable API key (hidden): " SUPABASE_ANON_KEY
printf '\n'

if [[ -z "$PRIMARY_PASSWORD" || -z "$PAPER_PASSWORD" || -z "$SUPABASE_ANON_KEY" ]]; then
  unset PRIMARY_PASSWORD PAPER_PASSWORD SUPABASE_ANON_KEY
  echo "BLOCKED: a required value was empty; no secret versions were added."
  exit 1
fi
if [[ "$PRIMARY_PASSWORD" == "$PAPER_PASSWORD" ]]; then
  unset PRIMARY_PASSWORD PAPER_PASSWORD SUPABASE_ANON_KEY
  echo "BLOCKED: the two database roles must have different passwords."
  exit 1
fi

url_encode_password() {
  DB_PASSWORD="$1" python - <<'PY'
import os
from urllib.parse import quote
print(quote(os.environ["DB_PASSWORD"], safe=""))
PY
}

PRIMARY_ENCODED="$(url_encode_password "$PRIMARY_PASSWORD")"
PAPER_ENCODED="$(url_encode_password "$PAPER_PASSWORD")"
PRIMARY_URL="postgresql+asyncpg://atlas_paper_reader.${SUPABASE_PROJECT_REF}:${PRIMARY_ENCODED}@${SUPABASE_DB_HOST}:5432/postgres?ssl=require"
PAPER_URL="postgresql+asyncpg://atlas_customer_paper.${SUPABASE_PROJECT_REF}:${PAPER_ENCODED}@${SUPABASE_DB_HOST}:5432/postgres?ssl=require"
unset PRIMARY_PASSWORD PAPER_PASSWORD PRIMARY_ENCODED PAPER_ENCODED

put_secret() {
  local secret_name="$1"
  local secret_value="$2"
  if ! gcloud secrets describe "$secret_name" --project="$PROJECT_ID" >/dev/null 2>&1; then
    gcloud secrets create "$secret_name" --replication-policy=automatic --project="$PROJECT_ID"
  fi
  printf '%s' "$secret_value" | gcloud secrets versions add "$secret_name" \
    --data-file=- --project="$PROJECT_ID" >/dev/null
  unset secret_value
}

put_secret "$PRIMARY_SECRET" "$PRIMARY_URL"
put_secret "$PAPER_SECRET" "$PAPER_URL"
put_secret "$ANON_KEY_SECRET" "$SUPABASE_ANON_KEY"
unset PRIMARY_URL PAPER_URL SUPABASE_ANON_KEY

# Cloud Run can read only its three explicitly referenced secret values.
for secret_name in "$PRIMARY_SECRET" "$PAPER_SECRET" "$ANON_KEY_SECRET"; do
  gcloud secrets add-iam-policy-binding "$secret_name" \
    --project="$PROJECT_ID" \
    --member="serviceAccount:${RUNTIME_SA}" \
    --role="roles/secretmanager.secretAccessor" --quiet
done

# The read-only preflight identity may inspect the two DB URLs but not the Supabase key.
for secret_name in "$PRIMARY_SECRET" "$PAPER_SECRET"; do
  gcloud secrets add-iam-policy-binding "$secret_name" \
    --project="$PROJECT_ID" \
    --member="serviceAccount:${PREFLIGHT_SA}" \
    --role="roles/secretmanager.secretAccessor" --quiet
done

echo
echo "PASS: staging GCP identities, repository, narrowly scoped IAM bindings, and Secret Manager versions created."
echo "Now add these exact values as variables in GitHub Settings > Environments > staging:"
printf 'GCP_PROJECT_ID=%s\n' "$PROJECT_ID"
printf 'GCP_REGION=%s\n' "$REGION"
printf 'GCP_WORKLOAD_IDENTITY_PROVIDER=projects/%s/locations/global/workloadIdentityPools/%s/providers/%s\n' "$PROJECT_NUMBER" "$POOL_ID" "$PROVIDER_ID"
printf 'GCP_STAGING_DEPLOY_SERVICE_ACCOUNT=%s\n' "$DEPLOY_SA"
printf 'GCP_STAGING_PREFLIGHT_SERVICE_ACCOUNT=%s\n' "$PREFLIGHT_SA"
printf 'ATLAS_STAGING_RUNTIME_SERVICE_ACCOUNT=%s\n' "$RUNTIME_SA"
printf 'ATLAS_STAGING_CLOUD_RUN_SERVICE=%s\n' "$CLOUD_RUN_SERVICE"
printf 'ATLAS_STAGING_DATABASE_SECRET=%s\n' "$PRIMARY_SECRET"
printf 'ATLAS_STAGING_PAPER_DATABASE_SECRET=%s\n' "$PAPER_SECRET"
printf 'ATLAS_STAGING_SUPABASE_ANON_KEY_SECRET=%s\n' "$ANON_KEY_SECRET"
printf 'ATLAS_PRODUCTION_SUPABASE_PROJECT_REF=%s\n' "$SUPABASE_PROJECT_REF"
printf 'ATLAS_STAGING_SUPABASE_PROJECT_REF=%s\n' "$SUPABASE_PROJECT_REF"
printf 'ATLAS_STAGING_RUNTIME_DB_ROLE=atlas_paper_reader\n'
printf 'ATLAS_STAGING_PAPER_DB_ROLE=atlas_customer_paper\n'
