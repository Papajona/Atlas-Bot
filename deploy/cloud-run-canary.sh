#!/usr/bin/env bash
set -euo pipefail

: "${CLOUD_RUN_SERVICE:?Set CLOUD_RUN_SERVICE}"
: "${GCP_REGION:?Set GCP_REGION}"
: "${IMAGE:?Set immutable IMAGE reference}"
: "${CANARY_URL:?Set the exact Cloud Run tagged canary URL}"
: "${EXPECTED_RELEASE_SHA:?Set EXPECTED_RELEASE_SHA}"

gcloud run deploy "$CLOUD_RUN_SERVICE" \
  --image "$IMAGE" \
  --region "$GCP_REGION" \
  --no-traffic \
  --tag canary

curl --fail --silent --show-error --max-time 20 \
  -H "X-Atlas-Expected-Release: $EXPECTED_RELEASE_SHA" \
  "$CANARY_URL/readyz"

echo "CANARY_READY: $CANARY_URL"
