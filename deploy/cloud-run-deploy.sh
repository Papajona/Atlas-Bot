#!/usr/bin/env bash
set -euo pipefail

: "${GCP_PROJECT_ID:?Set GCP_PROJECT_ID}"
: "${GCP_REGION:?Set GCP_REGION, e.g. europe-west1}"
: "${AR_REPOSITORY:?Set AR_REPOSITORY, e.g. trading}"
: "${CLOUD_RUN_SERVICE:?Set CLOUD_RUN_SERVICE, e.g. atlas-trading-api}"
: "${MODEL_BUCKET:?Set MODEL_BUCKET}"
: "${MODEL_STAGING_BUCKET:?Set MODEL_STAGING_BUCKET}"
: "${SERVICE_ACCOUNT:?Set SERVICE_ACCOUNT}"
: "${SECRET_DATABASE_VERSION:?Set SECRET_DATABASE_VERSION, e.g. 3}"
: "${SECRET_KEY_VERSION:?Set SECRET_KEY_VERSION, e.g. 7}"
: "${SECRET_TRON_XPUB_VERSION:?Set SECRET_TRON_XPUB_VERSION for the public TRON account xpub}"
: "${SECRET_APP_ENCRYPTION_VERSION:?Set SECRET_APP_ENCRYPTION_VERSION}"
: "${SECRET_REDIS_URL_VERSION:?Set SECRET_REDIS_URL_VERSION}"
: "${SUPABASE_URL:?Set SUPABASE_URL}"
: "${ADMIN_SUPABASE_USER_IDS:?Set ADMIN_SUPABASE_USER_IDS}"
: "${ADMIN_ROLE_ASSIGNMENTS:?Set ADMIN_ROLE_ASSIGNMENTS with at least one intended ADMINISTRATOR UUID:ROLE assignment}"
: "${BACKUP_RECOVERY_URL:?Set BACKUP_RECOVERY_URL}"
: "${SECRET_TRONGRID_VERSION:?Set SECRET_TRONGRID_VERSION, e.g. 2}"
: "${SECRET_MODEL_SIGNING_PUBLIC_VERSION:?Set SECRET_MODEL_SIGNING_PUBLIC_VERSION}"
: "${SECRET_SUPABASE_ANON_KEY_VERSION:?Set SECRET_SUPABASE_ANON_KEY_VERSION}"
: "${SECRET_FUNDING_WEBHOOK_VERSION:?Set SECRET_FUNDING_WEBHOOK_VERSION}"
: "${FORWARDED_ALLOW_IPS:?Set FORWARDED_ALLOW_IPS to trusted proxy IP/CIDR; never *}"
: "${RESEARCH_FEE_SOURCE:?Set RESEARCH_FEE_SOURCE to a traceable research-fee source}"
: "${RESEARCH_FEE_EVIDENCE_ID:?Set RESEARCH_FEE_EVIDENCE_ID to an immutable evidence identifier}"
: "${RESEARCH_MIN_DEFLATED_SHARPE:?Set RESEARCH_MIN_DEFLATED_SHARPE to at least 0.95}"

python - "${ADMIN_SUPABASE_USER_IDS}" "${ADMIN_ROLE_ASSIGNMENTS}" <<'PY'
import sys
from uuid import UUID

try:
    allowlisted = {str(UUID(item.strip())) for item in sys.argv[1].split(",") if item.strip()}
except ValueError:
    raise SystemExit("ERROR: ADMIN_SUPABASE_USER_IDS contains a non-UUID user ID")
raw_assignments = [item.strip() for item in sys.argv[2].split(",") if item.strip()]
if not allowlisted or not raw_assignments:
    raise SystemExit("ERROR: admin allow-list and role assignments must both be non-empty")
allowed_roles = {"READ_ONLY", "OPERATIONS", "RISK_OFFICER", "TREASURY", "COMPLIANCE", "FINANCE", "ADMINISTRATOR"}
seen = set()
has_admin = False
for assignment in raw_assignments:
    if ":" not in assignment:
        raise SystemExit("ERROR: each ADMIN_ROLE_ASSIGNMENTS entry must be UUID:ROLE")
    user_id, role = assignment.split(":", 1)
    user_id, role = user_id.strip(), role.strip().upper()
    try:
        canonical_id = str(UUID(user_id))
    except (ValueError, AttributeError):
        raise SystemExit("ERROR: ADMIN_ROLE_ASSIGNMENTS contains a non-UUID user ID")
    if role not in allowed_roles:
        raise SystemExit("ERROR: ADMIN_ROLE_ASSIGNMENTS contains an unsupported role")
    if canonical_id not in allowlisted:
        raise SystemExit("ERROR: every bootstrap role assignment must be present in ADMIN_SUPABASE_USER_IDS")
    if canonical_id in seen:
        raise SystemExit("ERROR: duplicate user in ADMIN_ROLE_ASSIGNMENTS")
    seen.add(canonical_id)
    has_admin = has_admin or role == "ADMINISTRATOR"
if not has_admin:
    raise SystemExit("ERROR: at least one explicit ADMINISTRATOR assignment is required")
PY

python - "${RESEARCH_MIN_DEFLATED_SHARPE}" <<'PY'
import math
import sys
try:
    threshold = float(sys.argv[1])
except (TypeError, ValueError):
    raise SystemExit("ERROR: RESEARCH_MIN_DEFLATED_SHARPE must be numeric and >= 0.95")
if not math.isfinite(threshold) or threshold < 0.95:
    raise SystemExit("ERROR: RESEARCH_MIN_DEFLATED_SHARPE must be numeric and >= 0.95")
PY
: "${SECRET_GEMINI_API_KEY_VERSION:?Set SECRET_GEMINI_API_KEY_VERSION}"
SECRET_GROQ_API_KEY_VERSION="${SECRET_GROQ_API_KEY_VERSION:-}"

# Paper mode does not disable TRON custody; use the shared fail-closed gate.
source "$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)/tron-funding-gate.sh"
atlas_configure_tron_funding

# Always starts with a comma: it is appended directly after the last fixed secret in --set-secrets.
AI_SECRET_ARGS=",GEMINI_API_KEY=atlas-gemini-api-key:${SECRET_GEMINI_API_KEY_VERSION}"
if [[ -z "$SECRET_GROQ_API_KEY_VERSION" ]]; then
  echo "WARNING: no Groq key. The AI safety veto requires BOTH Gemini and Groq to approve (fail-closed), so customer bot starts via the API will return NO_TRADE until GROQ is configured (set SECRET_GROQ_REF on the worker too)." >&2
fi
if [[ -n "$SECRET_GROQ_API_KEY_VERSION" ]]; then AI_SECRET_ARGS="${AI_SECRET_ARGS},GROQ_API_KEY=atlas-groq-api-key:${SECRET_GROQ_API_KEY_VERSION}"; fi

if [[ -n "$(git status --porcelain)" ]]; then
  echo "ERROR: deployment requires a clean git worktree; commit or discard local changes before building." >&2
  exit 2
fi
GIT_SHA="$(git rev-parse HEAD)"
RELEASE_VERSION="$(sed -n 's/^[[:space:]]*app_version: str = "\([^"]*\)".*/\1/p' app/config.py | head -n1)"
if [[ ! "$GIT_SHA" =~ ^[0-9a-f]{40}$ || -z "$RELEASE_VERSION" ]]; then
  echo "ERROR: unable to derive immutable release identity from git/app/config.py" >&2
  exit 2
fi
IMAGE_TAG="${GCP_REGION}-docker.pkg.dev/${GCP_PROJECT_ID}/${AR_REPOSITORY}/${CLOUD_RUN_SERVICE}:${GIT_SHA}"
SA="${SERVICE_ACCOUNT}@${GCP_PROJECT_ID}.iam.gserviceaccount.com"

gcloud config set project "$GCP_PROJECT_ID"
gcloud builds submit --tag "$IMAGE_TAG" .
IMAGE_DIGEST="$(gcloud artifacts docker images describe "$IMAGE_TAG" --format='value(image_summary.digest)')"
if [[ ! "$IMAGE_DIGEST" =~ ^sha256:[0-9a-f]{64}$ ]]; then
  echo "ERROR: Artifact Registry did not return a valid immutable image digest." >&2
  exit 2
fi
IMAGE="${GCP_REGION}-docker.pkg.dev/${GCP_PROJECT_ID}/${AR_REPOSITORY}/${CLOUD_RUN_SERVICE}@${IMAGE_DIGEST}"

ENV_VARS="^@^ENVIRONMENT=production@RELEASE_SHA=${GIT_SHA}@RELEASE_VERSION=${RELEASE_VERSION}@IMAGE_DIGEST=${IMAGE_DIGEST}@PAPER_TRADING=true@LIVE_TRADING_ENABLED=false@BROKER_SANDBOX=true@DOCS_ENABLED=false@METRICS_REQUIRE_ADMIN=true@BACKGROUND_RECONCILIATION_ENABLED=false@PROCESS_ROLE=api@MODEL_DIR=/app/models@USDT_TRON_ENABLED=${USDT_TRON_ENABLED}@USDT_TRON_NETWORK=${USDT_TRON_NETWORK}@USDT_TRON_SWEEP_ENABLED=false@USDT_TRONGRID_BASE_URL=https://api.trongrid.io@USDT_TRON_USDT_CONTRACT=TR7NHqjeKQxGTCi8q8ZY4pL8otSzgjLj6t@USDT_TRON_POLL_SECONDS=60@USDT_TRON_MIN_CONFIRMATIONS=19@FORWARDED_ALLOW_IPS=${FORWARDED_ALLOW_IPS}@SUPABASE_URL=${SUPABASE_URL}@ADMIN_SUPABASE_USER_IDS=${ADMIN_SUPABASE_USER_IDS}@ADMIN_ROLE_ASSIGNMENTS=${ADMIN_ROLE_ASSIGNMENTS}@BACKUP_RECOVERY_URL=${BACKUP_RECOVERY_URL}@ADMIN_TOTP_REQUIRED=true@DISTRIBUTED_RATE_LIMIT_REQUIRED=true@REQUIRE_BACKUP_RECOVERY_CONFIG=true@RESEARCH_FEE_SOURCE=${RESEARCH_FEE_SOURCE}@RESEARCH_FEE_EVIDENCE_ID=${RESEARCH_FEE_EVIDENCE_ID}@RESEARCH_MIN_DEFLATED_SHARPE=${RESEARCH_MIN_DEFLATED_SHARPE}@GEMINI_MODEL=${GEMINI_MODEL:-gemini-3.5-flash}@GEMINI_STRATEGY_MODEL=${GEMINI_STRATEGY_MODEL:-gemini-3.5-flash}@GROQ_MODEL=openai/gpt-oss-20b@GROQ_STRATEGY_MODEL=openai/gpt-oss-20b@GROQ_MAX_COMPLETION_TOKENS=4096"

gcloud run deploy "$CLOUD_RUN_SERVICE" \
  --image "$IMAGE" \
  --region "$GCP_REGION" \
  --execution-environment gen2 \
  --service-account "$SA" \
  --port 8000 \
  --min-instances 1 \
  --max-instances 5 \
  --concurrency 40 \
  --cpu 2 \
  --memory 2Gi \
  --timeout 300 \
  --set-env-vars "$ENV_VARS" \
  --set-secrets "DATABASE_URL=atlas-database-url:${SECRET_DATABASE_VERSION},SECRET_KEY=atlas-secret-key:${SECRET_KEY_VERSION},APP_ENCRYPTION_KEY=atlas-app-encryption-key:${SECRET_APP_ENCRYPTION_VERSION},REDIS_URL=atlas-redis-url:${SECRET_REDIS_URL_VERSION},USDT_TRON_ACCOUNT_XPUB=atlas-usdt-tron-account-xpub:${SECRET_TRON_XPUB_VERSION},MODEL_SIGNING_PUBLIC_KEY=atlas-model-signing-public-key:${SECRET_MODEL_SIGNING_PUBLIC_VERSION},SUPABASE_ANON_KEY=atlas-supabase-anon-key:${SECRET_SUPABASE_ANON_KEY_VERSION},FUNDING_WEBHOOK_SECRET=atlas-funding-webhook-secret:${SECRET_FUNDING_WEBHOOK_VERSION},USDT_TRONGRID_API_KEY=atlas-trongrid-api-key:${SECRET_TRONGRID_VERSION}${AI_SECRET_ARGS}" \
  --add-volume "mount-path=/app/models,type=cloud-storage,bucket=$MODEL_BUCKET,readonly=true,mount-options=uid=10001;gid=10001" \
  --ingress internal-and-cloud-load-balancing \
  --allow-unauthenticated

gcloud run services describe "$CLOUD_RUN_SERVICE" --region "$GCP_REGION" --format='value(status.url)'
