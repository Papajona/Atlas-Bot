#!/usr/bin/env bash
set -euo pipefail

: "${ATLAS_BASE_URL:?Set ATLAS_BASE_URL to the deployed Atlas HTTPS URL}"
: "${EXPECTED_RELEASE_SHA:?Set EXPECTED_RELEASE_SHA to the exact 40-character Git SHA}"
: "${EXPECTED_IMAGE_DIGEST:?Set EXPECTED_IMAGE_DIGEST to the exact sha256 image digest}"

if [[ ! "$EXPECTED_RELEASE_SHA" =~ ^[0-9a-f]{40}$ ]]; then
  echo "ERROR: EXPECTED_RELEASE_SHA must be a full 40-character Git SHA." >&2
  exit 2
fi
if [[ ! "$EXPECTED_IMAGE_DIGEST" =~ ^sha256:[0-9a-f]{64}$ ]]; then
  echo "ERROR: EXPECTED_IMAGE_DIGEST must be an immutable sha256 digest." >&2
  exit 2
fi

BASE="${ATLAS_BASE_URL%/}"
BODY="$(curl --fail --silent --show-error --location --max-time 15 "$BASE/healthz")"
export BODY EXPECTED_RELEASE_SHA EXPECTED_IMAGE_DIGEST

python - <<'PY'
import json
import os
import sys

try:
    data = json.loads(os.environ["BODY"])
except json.JSONDecodeError as exc:
    print(f"ERROR: /healthz did not return JSON: {exc}", file=sys.stderr)
    sys.exit(1)

if data.get("status") != "ok":
    print("ERROR: /healthz status is not ok.", file=sys.stderr)
    sys.exit(1)

release = data.get("release") or {}
actual_sha = release.get("sha")
actual_digest = release.get("image_digest")

if actual_sha != os.environ["EXPECTED_RELEASE_SHA"]:
    print(
        f"ERROR: deployed release SHA mismatch: expected {os.environ['EXPECTED_RELEASE_SHA']}, "
        f"got {actual_sha!r}.",
        file=sys.stderr,
    )
    sys.exit(1)

if actual_digest != os.environ["EXPECTED_IMAGE_DIGEST"]:
    print(
        f"ERROR: deployed image digest mismatch: expected {os.environ['EXPECTED_IMAGE_DIGEST']}, "
        f"got {actual_digest!r}.",
        file=sys.stderr,
    )
    sys.exit(1)

print(
    json.dumps(
        {
            "verified": True,
            "release_sha": actual_sha,
            "image_digest": actual_digest,
            "version": release.get("version"),
        },
        sort_keys=True,
    )
)
PY
