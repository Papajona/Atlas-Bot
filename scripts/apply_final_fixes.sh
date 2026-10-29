#!/usr/bin/env bash
set -euo pipefail

ROOT="$(git rev-parse --show-toplevel 2>/dev/null || pwd)"
cd "$ROOT"

PATCH_FILE="${1:-}"
if [[ -z "$PATCH_FILE" ]]; then
  echo "ERROR: pass the patch file explicitly: $0 /path/to/fixes.patch" >&2
  exit 2
fi
if [[ ! -f "$PATCH_FILE" ]]; then
  echo "ERROR: patch file not found: $PATCH_FILE" >&2
  exit 2
fi

patch -p1 < "$PATCH_FILE"

if git rev-parse --is-inside-work-tree >/dev/null 2>&1; then
  while IFS= read -r -d '' path; do
    git rm -r -q --cached --ignore-unmatch -- "$path" || true
  done < <(git ls-files -z | grep -zE '(^|/)__pycache__/|\.pyc$' || true)
fi

PYTHON_BIN="${PYTHON_BIN:-python3.13}"
if ! command -v "$PYTHON_BIN" >/dev/null 2>&1; then
  echo "ERROR: $PYTHON_BIN is required; Atlas CI and the production Dockerfile target Python 3.13." >&2
  exit 3
fi

echo "Run:"
echo "  $PYTHON_BIN -m venv .venv"
echo "  . .venv/bin/activate"
echo "  python -m pip install -r requirements-dev.txt"
echo "  python -m pytest -q"
echo "  ATLAS_POSTGRES_URL=... python -m pytest -q -m postgres"
echo
if [[ -d android ]]; then
  if [[ -d android/app/src/main/java ]]; then
    echo "Comparing canonical and legacy Android source trees:"
    diff -r app/src/main/java android/app/src/main/java || true
  fi
  echo "ERROR: legacy android/ tree exists; app/src/main/java is the only supported Android source tree." >&2
  exit 4
fi
echo "PASS: only the canonical app/src/main/java Android source tree exists."
