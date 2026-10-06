#!/usr/bin/env bash
set -euo pipefail

ROOT="$(git rev-parse --show-toplevel 2>/dev/null || pwd)"
cd "$ROOT"

PATCH_FILE="${1:-atlas_final_fixes.patch}"
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
echo "Android source comparison (the duplicate tree is app/src/main/java):"
echo "  diff -r app/src/main/java app/src/main/java"
