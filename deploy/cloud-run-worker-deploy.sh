#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd -- "${SCRIPT_DIR}/.." && pwd)"

: "${GCP_PROJECT_ID:?Set GCP_PROJECT_ID}"
: "${GCP_REGION:?Set GCP_REGION}"
: "${WORKER_POOL:?Set WORKER_POOL, e.g. atlas-trading-worker}"
: "${IMAGE:?Set IMAGE to the immutable Atlas image already built as an @sha256: digest}"
: "${RELEASE_SHA:?Set RELEASE_SHA to the full 40-character git SHA used to build IMAGE}"
: "${RELEASE_VERSION:?Set RELEASE_VERSION to the app/config.py release version}"
: "${IMAGE_DIGEST:?Set IMAGE_DIGEST to the sha256 digest returned by Artifact Registry}"
: "${WORKER_SERVICE_ACCOUNT:?Set WORKER_SERVICE_ACCOUNT}"
: "${DATABASE_SECRET_REF:?Set DATABASE_SECRET_REF, e.g. atlas-database-url:3}"
: "${SECRET_REDIS_URL_REF:?Set SECRET_REDIS_URL_REF, e.g. atlas-redis-url:3}"
: "${SECRET_KEY_REF:?Set SECRET_KEY_REF}"
: "${SECRET_APP_ENCRYPTION_REF:?Set SECRET_APP_ENCRYPTION_REF}"
: "${SECRET_TRON_XPUB_REF:?Set SECRET_TRON_XPUB_REF}"
: "${SECRET_TRONGRID_REF:?Set SECRET_TRONGRID_REF}"
: "${SECRET_MODEL_SIGNING_PUBLIC_REF:?Set SECRET_MODEL_SIGNING_PUBLIC_REF}"
: "${SECRET_MODEL_SIGNING_PRIVATE_REF:?Set SECRET_MODEL_SIGNING_PRIVATE_REF}"
: "${MODEL_BUCKET:?Set MODEL_BUCKET}"
: "${MODEL_STAGING_BUCKET:?Set MODEL_STAGING_BUCKET}"
: "${BACKUP_RECOVERY_URL:?Set BACKUP_RECOVERY_URL (startup refuses to run in production without it)}"
: "${RESEARCH_FEE_SOURCE:?Set RESEARCH_FEE_SOURCE to a traceable research-fee source}"
: "${RESEARCH_FEE_EVIDENCE_ID:?Set RESEARCH_FEE_EVIDENCE_ID to an immutable evidence identifier}"
: "${RESEARCH_MIN_DEFLATED_SHARPE:?Set RESEARCH_MIN_DEFLATED_SHARPE to a finite number >= 0.95}"

# Validate the comma-delimited environment values and production DSR floor before deployment.
python "$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)/validate_research_deploy_env.py" \
  "$RESEARCH_FEE_SOURCE" "$RESEARCH_FEE_EVIDENCE_ID" "$RESEARCH_MIN_DEFLATED_SHARPE" ","

# Worker runs the TRON listener/sweeper; share the API gate to avoid config drift.
source "$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)/tron-funding-gate.sh"
atlas_configure_tron_funding

if [[ ! "$IMAGE" =~ @sha256:[0-9a-f]{64}$ || ! "$IMAGE_DIGEST" =~ ^sha256:[0-9a-f]{64}$ || "$IMAGE" != *@$IMAGE_DIGEST ]]; then
  echo "ERROR: worker IMAGE must be an immutable @sha256 digest matching IMAGE_DIGEST." >&2
  exit 2
fi
if [[ ! "$RELEASE_SHA" =~ ^[0-9a-f]{40}$ ]]; then
  echo "ERROR: RELEASE_SHA must be a full 40-character git SHA." >&2
  exit 2
fi

IMAGE_TAG_SHA="${IMAGE%@*}"
IMAGE_TAG_SHA="${IMAGE_TAG_SHA##*:}"
if [[ "$IMAGE_TAG_SHA" != "$RELEASE_SHA" ]]; then
  echo "ERROR: worker image tag commit ($IMAGE_TAG_SHA) does not match RELEASE_SHA ($RELEASE_SHA)." >&2
  exit 2
fi
SA="${WORKER_SERVICE_ACCOUNT}@${GCP_PROJECT_ID}.iam.gserviceaccount.com"

# Atlas Native AI remains the strategy/safety authority; Groq is an optional research-only reviewer.
SECRET_GROQ_REF="${SECRET_GROQ_REF:-}"
if [[ -n "$SECRET_GROQ_REF" && ! "$SECRET_GROQ_REF" =~ ^atlas-groq-api-key:[1-9][0-9]*$ ]]; then
  echo "ERROR: SECRET_GROQ_REF must be atlas-groq-api-key:VERSION with a positive numeric version." >&2
  exit 2
fi
GROQ_SECRET_ARG=""
if [[ -n "$SECRET_GROQ_REF" ]]; then GROQ_SECRET_ARG=",GROQ_API_KEY=${SECRET_GROQ_REF}"; fi
: "${GROQ_MODEL:?Set GROQ_MODEL to the same approved value used by the API deployment}"
: "${GROQ_MAX_COMPLETION_TOKENS:?Set GROQ_MAX_COMPLETION_TOKENS to the same approved API value}"
: "${GROQ_RESEARCH_REVIEW_ENABLED:?Set GROQ_RESEARCH_REVIEW_ENABLED to the same approved API value}"
GROQ_RESEARCH_REVIEW_ENABLED="${GROQ_RESEARCH_REVIEW_ENABLED,,}"
GROQ_RESEARCH_REVIEW_ENABLED="${GROQ_RESEARCH_REVIEW_ENABLED,,}"
python "${REPO_ROOT}/scripts/validate_groq_deploy_env.py" \
  "$GROQ_MODEL" "$GROQ_MAX_COMPLETION_TOKENS" "$GROQ_RESEARCH_REVIEW_ENABLED"

gcloud run worker-pools deploy "$WORKER_POOL" \
  --project "$GCP_PROJECT_ID" \
  --region "$GCP_REGION" \
  --image "$IMAGE" \
  --service-account "$SA" \
  --instances 1 \
  --cpu 2 \
  --memory 2Gi \
  --command python \
  --args=-m,app.worker \
  --set-env-vars "ENVIRONMENT=production,PROCESS_ROLE=worker,RELEASE_SHA=${RELEASE_SHA},RELEASE_VERSION=${RELEASE_VERSION},IMAGE_DIGEST=${IMAGE_DIGEST},BACKUP_RECOVERY_URL=${BACKUP_RECOVERY_URL},OMP_NUM_THREADS=2,ML_THREADS=2,PAPER_TRADING=true,LIVE_TRADING_ENABLED=false,CUSTOMER_LIVE_TRADING_ENABLED=false,PAYOUT_LIVE_ENABLED=false,BROKER_SANDBOX=true,DOCS_ENABLED=false,MODEL_DIR=/app/models,MODEL_STAGING_DIR=/app/model-staging,RESEARCH_FEE_SOURCE=${RESEARCH_FEE_SOURCE},RESEARCH_FEE_EVIDENCE_ID=${RESEARCH_FEE_EVIDENCE_ID},RESEARCH_MIN_DEFLATED_SHARPE=${RESEARCH_MIN_DEFLATED_SHARPE},USDT_TRON_ENABLED=${USDT_TRON_ENABLED},USDT_TRON_TREASURY_ADDRESS_ROLE=${USDT_TRON_TREASURY_ADDRESS_ROLE:-unknown},USDT_TRON_NETWORK=${USDT_TRON_NETWORK},USDT_TRON_SWEEP_ENABLED=false,USDT_TRONGRID_BASE_URL=https://api.trongrid.io,USDT_TRON_USDT_CONTRACT=TR7NHqjeKQxGTCi8q8ZY4pL8otSzgjLj6t,USDT_TRON_POLL_SECONDS=60,USDT_TRON_MIN_CONFIRMATIONS=19,DISTRIBUTED_RATE_LIMIT_REQUIRED=true,EXTERNAL_SECURITY_AUDIT_ENABLED=true,EXTERNAL_SECURITY_AUDIT_REQUIRED=true,GROQ_MODEL=${GROQ_MODEL},GROQ_MAX_COMPLETION_TOKENS=${GROQ_MAX_COMPLETION_TOKENS},GROQ_RESEARCH_REVIEW_ENABLED=${GROQ_RESEARCH_REVIEW_ENABLED}" \
  --set-secrets "DATABASE_URL=$DATABASE_SECRET_REF,SECRET_KEY=$SECRET_KEY_REF,APP_ENCRYPTION_KEY=$SECRET_APP_ENCRYPTION_REF,REDIS_URL=$SECRET_REDIS_URL_REF,USDT_TRON_ACCOUNT_XPUB=$SECRET_TRON_XPUB_REF,USDT_TRONGRID_API_KEY=$SECRET_TRONGRID_REF,MODEL_SIGNING_PUBLIC_KEY=$SECRET_MODEL_SIGNING_PUBLIC_REF,MODEL_SIGNING_PRIVATE_KEY=$SECRET_MODEL_SIGNING_PRIVATE_REF${GROQ_SECRET_ARG}" \
  --add-volume "mount-path=/app/models,type=cloud-storage,bucket=$MODEL_BUCKET,readonly=false,mount-options=uid=10001;gid=10001" \
  --add-volume "mount-path=/app/model-staging,type=cloud-storage,bucket=$MODEL_STAGING_BUCKET,readonly=false,mount-options=uid=10001;gid=10001"
