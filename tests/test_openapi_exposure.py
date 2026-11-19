"""The unauthenticated OpenAPI schema must follow DOCS_ENABLED, not just /docs and /redoc."""
import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

_PROBE = (
    "import warnings; warnings.simplefilter('ignore')\n"
    "from fastapi.testclient import TestClient\n"
    "from app.main import app\n"
    "c = TestClient(app, raise_server_exceptions=False)\n"
    "print(app.openapi_url, c.get('/openapi.json').status_code, c.get('/docs').status_code)\n"
)


def _probe(docs_enabled: str) -> str:
    env = {
        "PATH": os.environ.get("PATH", ""),
        "HOME": os.environ.get("HOME", ""),
        "PYTHONPATH": str(ROOT),
        "MULTIDICT_NO_EXTENSIONS": "1",
        "ENVIRONMENT": "development",
        "DATABASE_URL": "sqlite+aiosqlite:///:memory:",
        "PAPER_TRADING": "true",
        "DOCS_ENABLED": docs_enabled,
    }
    done = subprocess.run([sys.executable, "-c", _PROBE], env=env, cwd=ROOT, capture_output=True, text=True, timeout=120)
    assert done.returncode == 0, done.stderr[-800:]
    return done.stdout.strip().splitlines()[-1]


def test_openapi_schema_is_not_served_when_docs_are_disabled():
    assert _probe("false") == "None 404 404"


def test_openapi_schema_is_served_only_when_docs_are_explicitly_enabled():
    assert _probe("true") == "/openapi.json 200 200"
