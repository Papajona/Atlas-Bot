#!/usr/bin/env bash
set -euo pipefail

# Run this from Cloud Shell/Compute through a loopback-bound Cloud SQL Auth Proxy/tunnel
# to a disposable/staging PostgreSQL database. Direct remote hostnames are rejected.
# It does not enable live trading.
: "${DATABASE_URL:?Set DATABASE_URL to a PostgreSQL database}"

# Alembic uses SQLAlchemy's asyncpg driver. Normalize legacy PostgreSQL URL
# schemes and libpq sslmode query parameters to asyncpg's ssl parameter before
# the database guard and Alembic consume the URL.
DATABASE_URL="$(python scripts/normalize_asyncpg_database_url.py "$DATABASE_URL")"
export DATABASE_URL

# Reuse the strict verification guard, but disable the Compose-only hostname
# exception: this Cloud Shell/Compute workflow must use a loopback proxy.
unset ATLAS_VERIFY_COMPOSE_POSTGRES_SERVICE
python scripts/verify_release_database_guard.py

export ENVIRONMENT=development
export PAPER_TRADING=true
export LIVE_TRADING_ENABLED=false
export BROKER_SANDBOX=true
export BACKGROUND_RECONCILIATION_ENABLED=false
export ATLAS_POSTGRES_URL="$DATABASE_URL"

python -m alembic upgrade head
python -m alembic check
DATABASE_URL="$DATABASE_URL" python deploy/validate-postgres-constraints.py
python -m pytest -q tests/test_postgres_concurrency_3_10_39.py
python -m pytest -q tests/test_postgres_ledger_concurrency_3_10_46.py  # real ledger code under contention (the 3_10_39 test only exercises a probe table)
python -m pytest -q tests/test_live_money_hardening_3_10_38.py tests/test_live_money_static_3_10_38.py tests/test_execution_control_plane_3_10_43.py tests/test_research_validation.py tests/test_adaptive_strategy_router_3_10_41.py tests/test_trade_learning_3_10_42.py

if command -v bandit >/dev/null 2>&1; then
  python -m bandit -r app -lll
else
  echo "Bandit not installed; install requirements-dev.txt before production certification." >&2
fi
if command -v pip-audit >/dev/null 2>&1; then
  python -m pip_audit -r requirements.txt --strict
else
  echo "pip-audit not installed; install requirements-dev.txt before production certification." >&2
fi

echo
echo "POSTGRES RUNTIME VERIFICATION COMPLETED. Live trading remains disabled by this script."
