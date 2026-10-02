#!/usr/bin/env bash
set -euo pipefail

: "${GCP_PROJECT_ID:?Set GCP_PROJECT_ID}"
: "${GCP_REGION:?Set GCP_REGION}"
: "${WORKER_POOL:?Set WORKER_POOL, e.g. atlas-trading-worker}"
: "${IMAGE:?Set IMAGE to the immutable Atlas image already built}"
: "${WORKER_SERVICE_ACCOUNT:?Set WORKER_SERVICE_ACCOUNT}"
: "${DATABASE_SECRET_REF:?Set DATABASE_SECRET_REF, e.g. atlas-database-url:3}"
: "${SECRET_REDIS_URL_REF:?Set SECRET_REDIS_URL_REF, e.g. atlas-redis-url:3}"
: "${SECRET_KEY_REF:?Set SECRET_KEY_REF}"
: "${SECRET_APP_ENCRYPTION_REF:?Set SECRET_APP_ENCRYPTION_REF}"
: "${SECRET_TRON_XPUB_REF:?Set SECRET_TRON_XPUB_REF}"
: "${SECRET_TRONGRID_REF:?Set SECRET_TRONGRID_REF}"
: "${SECRET_MODEL_SIGNING_PUBLIC_REF:?Set SECRET_MODEL_SIGNING_PUBLIC_REF}"
: "${SECRET_MODEL_SIGNING_PRIVATE_REF:?Set SECRET_MODEL_SIGNING_PRIVATE_REF}"
: "${SECRET_GEMINI_REF:?Set SECRET_GEMINI_REF}"
: "${MODEL_BUCKET:?Set MODEL_BUCKET}"
: "${MODEL_STAGING_BUCKET:?Set MODEL_STAGING_BUCKET}"
: "${BACKUP_RECOVERY_URL:?Set BACKUP_RECOVERY_URL (startup refuses to run in production without it)}"

SA="${WORKER_SERVICE_ACCOUNT}@${GCP_PROJECT_ID}.iam.gserviceaccount.com"

# The persisted bot cycles run HERE (worker), and each one needs BOTH Gemini and Groq to approve (fail-closed). Without a Groq key every
# bot cycle ends in NO_TRADE / ai_safety_not_configured. Provide SECRET_GROQ_REF (e.g. atlas-groq-api-key:3) unless that is what you want.
SECRET_GROQ_REF="${SECRET_GROQ_REF:-}"
GROQ_SECRET_ARG=""
if [[ -n "$SECRET_GROQ_REF" ]]; then GROQ_SECRET_ARG=",GROQ_API_KEY=${SECRET_GROQ_REF}"; else
  echo "WARNING: SECRET_GROQ_REF not set. Worker bot cycles will never trade: the dual-AI safety veto requires both providers." >&2
fi

gcloud config set project "$GCP_PROJECT_ID"
gcloud run worker-pools deploy "$WORKER_POOL" \
  --region "$GCP_REGION" \
  --image "$IMAGE" \
  --service-account "$SA" \
  --instances 1 \
  --cpu 2 \
  --memory 2Gi \
  --command python \
  --args=-m,app.worker \
  --set-env-vars "ENVIRONMENT=production,PROCESS_ROLE=worker,BACKUP_RECOVERY_URL=${BACKUP_RECOVERY_URL},PAPER_TRADING=true,LIVE_TRADING_ENABLED=false,BROKER_SANDBOX=true,DOCS_ENABLED=false,MODEL_DIR=/app/models,MODEL_STAGING_DIR=/app/model-staging,USDT_TRON_ENABLED=true,USDT_TRON_NETWORK=mainnet,USDT_TRONGRID_BASE_URL=https://api.trongrid.io,USDT_TRON_USDT_CONTRACT=TR7NHqjeKQxGTCi8q8ZY4pL8otSzgjLj6t,USDT_TRON_POLL_SECONDS=60,USDT_TRON_MIN_CONFIRMATIONS=19,DISTRIBUTED_RATE_LIMIT_REQUIRED=true,EXTERNAL_SECURITY_AUDIT_ENABLED=true,EXTERNAL_SECURITY_AUDIT_REQUIRED=true,GEMINI_MODEL=${GEMINI_MODEL:-gemini-3.5-flash},GEMINI_STRATEGY_MODEL=${GEMINI_STRATEGY_MODEL:-gemini-3.5-flash},GROQ_MODEL=${GROQ_MODEL:-openai/gpt-oss-20b},GROQ_STRATEGY_MODEL=${GROQ_STRATEGY_MODEL:-openai/gpt-oss-20b},GROQ_MAX_COMPLETION_TOKENS=4096" \
  --set-secrets "DATABASE_URL=$DATABASE_SECRET_REF,SECRET_KEY=$SECRET_KEY_REF,APP_ENCRYPTION_KEY=$SECRET_APP_ENCRYPTION_REF,REDIS_URL=$SECRET_REDIS_URL_REF,USDT_TRON_ACCOUNT_XPUB=$SECRET_TRON_XPUB_REF,USDT_TRONGRID_API_KEY=$SECRET_TRONGRID_REF,MODEL_SIGNING_PUBLIC_KEY=$SECRET_MODEL_SIGNING_PUBLIC_REF,MODEL_SIGNING_PRIVATE_KEY=$SECRET_MODEL_SIGNING_PRIVATE_REF,GEMINI_API_KEY=$SECRET_GEMINI_REF${GROQ_SECRET_ARG}" \
  --add-volume "mount-path=/app/models,type=cloud-storage,bucket=$MODEL_BUCKET,readonly=false,mount-options=uid=10001;gid=10001" \
  --add-volume "mount-path=/app/model-staging,type=cloud-storage,bucket=$MODEL_STAGING_BUCKET,readonly=false,mount-options=uid=10001;gid=10001"
