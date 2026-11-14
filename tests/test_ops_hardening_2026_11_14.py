"""Regression tests for the 2026-11-14 readiness hardening.

The readiness cache is intentionally tested as process-local single-flight state.
"""

import asyncio
import pathlib
import re
import uuid
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from app import main, monitoring, paper_staging
from app.config import Settings

ROOT = pathlib.Path(__file__).resolve().parents[1]


def _series_count() -> int:
    return len(monitoring.REQUESTS._metrics) + len(monitoring.REQUEST_LATENCY._metrics)


def test_metric_labels_use_route_template_not_raw_path():
    client = TestClient(main.app)
    client.get("/healthz")
    before = _series_count()
    for _ in range(30):
        client.get(f"/api/does-not-exist/{uuid.uuid4().hex}")
    after = _series_count()
    labels = {key[1] for key in monitoring.REQUESTS._metrics}
    assert after - before <= 4
    assert "unmatched" in labels
    assert not any("does-not-exist" in label for label in labels)


def test_metric_path_label_uses_mount_path_for_static_assets():
    mounted_request = SimpleNamespace(
        scope={
            "type": "http",
            "path": "/static/assets/dashboard.js",
            "root_path": "",
            "path_params": {},
        },
        app=main.app,
    )
    unmatched_request = SimpleNamespace(
        scope={
            "type": "http",
            "path": "/not-a-mounted-resource/opaque",
            "root_path": "",
            "path_params": {},
        },
        app=main.app,
    )

    assert monitoring.metric_path_label(mounted_request) == "/static"
    assert monitoring.metric_path_label(unmatched_request) == "unmatched"


def test_request_id_is_sanitised():
    client = TestClient(main.app)
    good = "req-12345678.abc_DEF"
    assert client.get("/healthz", headers={"x-request-id": good}).headers["x-request-id"] == good
    for bad in ("A" * 5000, "short", "has space in it 12345", "x" * 65, "bad$chars!!"):
        echoed = client.get("/healthz", headers={"x-request-id": bad}).headers["x-request-id"]
        assert echoed != bad
        assert re.fullmatch(r"[0-9a-f]{32}", echoed)
    assert re.fullmatch(r"[0-9a-f]{32}", client.get("/healthz").headers["x-request-id"])


def test_safe_request_id_unit():
    assert monitoring.safe_request_id(None) != monitoring.safe_request_id(None)
    assert monitoring.safe_request_id("abcDEF12-_.") == "abcDEF12-_."
    assert monitoring.safe_request_id("a\nb" * 10) != "a\nb" * 10


def test_healthz_keeps_release_identity_but_hides_worker_error_detail(monkeypatch):
    # healthz calls the snapshot helper directly; patch that exact call site.
    monkeypatch.setattr(
        main,
        "_worker_loop_health_snapshot",
        lambda: {
            "risk": {
                "status": "RESTARTING",
                "restart_count": 3,
                "last_error_type": "ConnectionError",
                "last_failure_at": "2026-11-14T00:00:00+00:00",
            }
        },
    )
    body = TestClient(main.app).get("/healthz").json()
    assert set(body["release"]) == {"version", "sha", "image_digest"}
    assert body["status"] == "degraded"
    assert body["worker_loops"] == {"risk": {"status": "RESTARTING", "restart_count": 3}}


def test_readyz_returns_503_when_any_check_fails(monkeypatch):
    async def not_ready():
        return {"ready": False, "checks": {"database": False}}

    monkeypatch.setattr(main, "_compute_readyz", not_ready)
    monkeypatch.setattr(main.settings, "readyz_cache_seconds", 0.0)
    monkeypatch.setattr(main.settings, "environment", "production")
    response = TestClient(main.app).get("/readyz")
    assert response.status_code == 503
    assert response.json()["ready"] is False
    assert response.headers["cache-control"] == "no-store"


def test_readyz_503_compatibility_switch_is_explicit(monkeypatch):
    async def not_ready():
        return {"ready": False, "checks": {}}

    monkeypatch.setattr(main, "_compute_readyz", not_ready)
    monkeypatch.setattr(main.settings, "readyz_cache_seconds", 0.0)
    monkeypatch.setattr(main.settings, "environment", "production")
    monkeypatch.setattr(main.settings, "readyz_fail_when_not_ready", False)
    assert TestClient(main.app).get("/readyz").status_code == 200


def test_readyz_status_depends_on_deployment_environment(monkeypatch):
    async def not_ready():
        return {"ready": False, "checks": {}}

    monkeypatch.setattr(main, "_compute_readyz", not_ready)
    monkeypatch.setattr(main.settings, "readyz_cache_seconds", 0.0)
    client = TestClient(main.app)
    monkeypatch.setattr(main.settings, "environment", "development")
    assert client.get("/readyz").status_code == 200
    monkeypatch.setattr(main.settings, "environment", "staging")
    assert client.get("/readyz").status_code == 503


def test_readyz_single_flight_cache_and_zero_ttl(monkeypatch):
    calls = {"count": 0}
    monkeypatch.setattr(main, "_readyz_cache", {"at": 0.0, "payload": None})
    monkeypatch.setattr(main, "_readyz_lock", asyncio.Lock())
    monkeypatch.setattr(main.settings, "readyz_cache_seconds", 30.0)

    async def slow_ready():
        calls["count"] += 1
        await asyncio.sleep(0.05)
        return {"ready": True, "checks": {"database": True}}

    monkeypatch.setattr(main, "_compute_readyz", slow_ready)

    async def exercise():
        responses = await asyncio.gather(*(main.readyz() for _ in range(25)))
        assert all(response.status_code == 200 for response in responses)
        assert calls["count"] == 1

        monkeypatch.setattr(main.settings, "readyz_cache_seconds", 0.0)
        await main.readyz()
        await main.readyz()
        assert calls["count"] == 3

    asyncio.run(exercise())


def test_readyz_body_still_carries_safety_checks():
    source = (ROOT / "app/main.py").read_text(encoding="utf-8")
    block = source[source.index('@app.get("/readyz")'):source.index('@app.get("/metrics")')]
    for name in ("execution_fencing", "migrations_current", "release_identity", "safe_trading_config"):
        assert f'checks["{name}"]' in block


def test_readyz_settings_are_declared_and_fail_closed_by_default(monkeypatch):
    monkeypatch.delenv("READYZ_CACHE_SECONDS", raising=False)
    settings = Settings(environment="test")
    assert settings.readyz_cache_seconds == 5.0
    assert settings.readyz_fail_when_not_ready is True


@pytest.mark.parametrize("invalid", [-1, float("nan"), float("inf"), float("-inf")])
def test_readyz_cache_ttl_must_be_finite_and_non_negative(invalid):
    with pytest.raises(ValidationError):
        Settings(environment="test", readyz_cache_seconds=invalid)


def test_primary_reader_preflight_uses_static_sql():
    source = (ROOT / "app/paper_staging.py").read_text(encoding="utf-8")
    start = source.index("async def _primary_reader_preflight")
    body = source[start:start + 12000]
    assert 'text("""' in body
    assert "Template(" not in body
    assert "substitute(" not in body
    assert "member_role.rolname='postgres'" in body
    assert "grantor_role.rolname='supabase_admin'" in body
    assert "m.admin_option" in body


def test_primary_reader_allowlist_matches_postgres_behavioral_fixture():
    source = (ROOT / "app/paper_staging.py").read_text(encoding="utf-8")
    preflight_start = source.index("async def _primary_reader_preflight")
    body = source[preflight_start:preflight_start + 12000]
    query_start = body.index('text("""') + len('text("""')
    query_end = body.index('"""', query_start)
    query = body[query_start:query_end]

    predicate_start = query.index("NOT (", query.index("AS memberships"))
    open_paren = query.index("(", predicate_start)
    depth = 0
    in_string = False
    predicate_end = None
    index = open_paren
    while index < len(query):
        char = query[index]
        if char == "'":
            if in_string and index + 1 < len(query) and query[index + 1] == "'":
                index += 2
                continue
            in_string = not in_string
        elif not in_string:
            if char == "(":
                depth += 1
            elif char == ")":
                depth -= 1
                if depth == 0:
                    predicate_end = index + 1
                    break
        index += 1

    assert predicate_end is not None, "production membership predicate must be balanced"
    production_predicate = query[predicate_start:predicate_end]
    def normalize(value):
        return re.sub(r"\s+", " ", value).strip()
    assert normalize(production_predicate) == normalize(
        paper_staging._UNSAFE_SUPABASE_MANAGED_MEMBERSHIP_PREDICATE
    )


def test_ci_scans_medium_severity_and_pins_runners():
    ci = (ROOT / ".github/workflows/ci.yml").read_text(encoding="utf-8")
    assert "bandit -r app -ll" in ci and "-lll" not in ci
    assert "ubuntu-latest" not in ci
    assert "bandit -r app -ll" in (ROOT / ".github/workflows/production-verification.yml").read_text(encoding="utf-8")
    assert "bandit -r app -ll" in (ROOT / "scripts/verify_release.sh").read_text(encoding="utf-8")
