#!/usr/bin/env bash
set -euo pipefail

: "${CLOUD_RUN_SERVICE:?Set CLOUD_RUN_SERVICE}"
: "${GCP_REGION:?Set GCP_REGION}"
: "${PREVIOUS_REVISION:?Set the verified previous Cloud Run revision}"
: "${ROLLBACK_CONFIRM:?Set ROLLBACK_CONFIRM=ROLLBACK_ATLAS to execute}"

if [[ "$ROLLBACK_CONFIRM" != "ROLLBACK_ATLAS" ]]; then
  echo "Refusing rollback: set ROLLBACK_CONFIRM=ROLLBACK_ATLAS" >&2
  exit 2
fi

gcloud run services update-traffic "$CLOUD_RUN_SERVICE" \
  --region "$GCP_REGION" \
  --to-revisions "$PREVIOUS_REVISION=100"

gcloud run services describe "$CLOUD_RUN_SERVICE" \
  --region "$GCP_REGION" \
  --format='value(status.traffic)'
