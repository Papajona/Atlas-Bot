"""scripts/paper_preflight.py: each check must fail for the reason it names and only that reason."""
import importlib.util

import pytest
from pathlib import Path
from types import SimpleNamespace


_SPEC = importlib.util.spec_from_file_location("paper_preflight", Path(__file__).resolve().parents[1] / "scripts" / "paper_preflight.py")
pf = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(pf)


def _settings(**over):
    base = dict(
        environment="development", paper_trading=True, broker_sandbox=True, secret_key="",
        live_trading_enabled=False, customer_live_trading_enabled=False, payout_live_enabled=False,
        usdt_tron_enabled=False, usdt_tron_sweep_enabled=False, deriv_live_enabled=False,
        forex_live_enabled=False, binance_arbitrage_live_enabled=False,
        external_custody_signer_required=False, external_custody_signer_url="", external_custody_signer_hmac_secret="",
        external_security_audit_required=True, external_security_audit_enabled=True,
        admin_supabase_user_ids="", customer_paper_ledger_enabled=False, customer_paper_database_url="",
        customer_paper_shared_database_enabled=False, customer_totp_required=True,
        api_rate_limit_per_minute=120, expensive_api_rate_limit_per_minute=12, auth_rate_limit_per_minute=10,
        max_request_body_bytes=2097152, paper_execution_evidence_path="",
        readyz_fail_when_not_ready=True, app_version="1.2.3", release_sha="abc123", release_version="1.2.3", image_digest="sha256:" + "a" * 64,
    )
    base.update(over)
    return SimpleNamespace(**base)


def _run(settings, **kw):
    kw.setdefault("encryption_check", lambda: None)
    kw.setdefault("calibration_check", lambda _s: None)
    return pf.check_settings(settings, **kw)


def _status(results, check):
    return [s for s, c, _ in results if c == check]


def test_clean_paper_config_has_no_failures():
    assert not [r for r in _run(_settings()) if r[0] == pf.FAIL]


@pytest.mark.parametrize("flag", [
    "live_trading_enabled", "customer_live_trading_enabled", "payout_live_enabled", "usdt_tron_enabled",
    "usdt_tron_sweep_enabled", "deriv_live_enabled", "forex_live_enabled", "binance_arbitrage_live_enabled",
])
def test_every_money_path_flag_fails_a_paper_run(flag):
    results = _run(_settings(**{flag: True}))
    assert _status(results, "money_paths_off") == [pf.FAIL]
    assert flag.upper() in next(d for s, c, d in results if c == "money_paths_off")


def test_paper_off_or_sandbox_off_fails():
    assert _status(_run(_settings(paper_trading=False)), "paper_trading") == [pf.FAIL]
    assert _status(_run(_settings(broker_sandbox=False)), "sandbox_when_paper") == [pf.FAIL]


def test_encryption_failure_is_reported_without_echoing_the_message():
    def boom():
        raise RuntimeError("secret-key-material-abc123")
    results = _run(_settings(), encryption_check=boom)
    detail = next(d for s, c, d in results if c == "encryption_configured")
    assert _status(results, "encryption_configured") == [pf.FAIL]
    assert "abc123" not in detail


def test_unconfigured_required_custody_signer_fails_readiness_preflight():
    results = _run(_settings(external_custody_signer_required=True))
    assert _status(results, "custody_signer_configured") == [pf.FAIL]
    ok = _run(_settings(external_custody_signer_required=True, external_custody_signer_url="https://s", external_custody_signer_hmac_secret="x"))
    assert _status(ok, "custody_signer_configured") == [pf.PASS]


def test_staging_requires_secret_and_admin_ids_like_readyz():
    results = _run(_settings(environment="staging"))
    assert _status(results, "secret_configured") == [pf.FAIL]
    assert _status(results, "admin_auth") == [pf.FAIL]
    ok = _run(_settings(environment="staging", secret_key="s", admin_supabase_user_ids="u1"))
    assert _status(ok, "secret_configured") == [] and _status(ok, "admin_auth") == [pf.PASS]


def test_production_only_checks_do_not_apply_to_staging():
    prod = _run(_settings(environment="production", secret_key="s", admin_supabase_user_ids="u", distributed_rate_limit_required=True, redis_url="", require_backup_recovery_config=True, backup_recovery_url=""))
    assert _status(prod, "distributed_rate_limiter") == [pf.FAIL] and _status(prod, "recovery_configured") == [pf.FAIL]
    stg = _run(_settings(environment="staging", secret_key="s", admin_supabase_user_ids="u", distributed_rate_limit_required=True, redis_url="", require_backup_recovery_config=True, backup_recovery_url=""))
    assert _status(stg, "distributed_rate_limiter") == [] and _status(stg, "recovery_configured") == []


def test_partial_calibration_fails_and_complete_calibration_passes():
    def partial(_s):
        raise ValueError("paper execution calibration configuration is incomplete: paper_impact_coefficient")
    assert _status(_run(_settings(), calibration_check=partial), "paper_fill_calibration") == [pf.FAIL]
    assert _status(_run(_settings(), calibration_check=lambda _s: {"ok": 1}), "paper_fill_calibration") == [pf.PASS]
    missing = _run(_settings(paper_execution_evidence_path="/nonexistent/evidence.json"), calibration_check=lambda _s: {"ok": 1})
    assert _status(missing, "paper_fill_calibration") == [pf.WARN]


def test_ledger_enabled_needs_an_explicit_database_target_even_in_shared_mode():
    assert _status(_run(_settings(customer_paper_ledger_enabled=True)), "customer_paper_ledger") == [pf.FAIL]
    assert _status(_run(_settings(customer_paper_ledger_enabled=True, customer_paper_shared_database_enabled=True)), "customer_paper_ledger") == [pf.FAIL]
    separate = _run(_settings(customer_paper_ledger_enabled=True, customer_paper_database_url="postgresql+asyncpg://u:p@h/d"))
    assert _status(separate, "customer_paper_ledger") == [pf.PASS]
    shared = _run(_settings(customer_paper_ledger_enabled=True, customer_paper_database_url="postgresql+asyncpg://u:p@h/d", customer_paper_shared_database_enabled=True))
    assert _status(shared, "customer_paper_ledger") == [pf.PASS]


def test_plain_postgresql_ledger_url_is_normalized_for_async_engine():
    from sqlalchemy.engine import make_url

    plain = pf._async_engine_url("postgresql://atlas:secret@db.example:5432/atlas")
    already_async = pf._async_engine_url("postgresql+asyncpg://atlas:secret@db.example:5432/atlas")
    assert make_url(plain).drivername == "postgresql+asyncpg"
    assert make_url(already_async).drivername == "postgresql+asyncpg"


def test_shared_mode_never_infers_the_primary_database_as_the_paper_target():
    settings = _settings(customer_paper_ledger_enabled=True, customer_paper_shared_database_enabled=True)
    assert not pf._text(pf._s(settings, "customer_paper_database_url"))


def test_database_passwords_are_never_printed():
    assert "hunter2" not in pf._safe_url("postgresql+asyncpg://atlas:hunter2@db.example:5432/atlas")
    assert "hunter2" not in pf._safe_url("postgresql+asyncpg://atlas@db.example:5432/atlas?password=hunter2&sslmode=require")
    assert "secret-token" not in pf._safe_url("postgresql://atlas@db.example/atlas?api_key=secret-token")


def test_settings_load_error_does_not_echo_exception_values(monkeypatch):
    import builtins
    original_import = builtins.__import__

    def fail_config_import(name, *args, **kwargs):
        if name == "app.config":
            raise RuntimeError("APP_SECRET=must-not-be-printed")
        return original_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", fail_config_import)
    _, results = pf._load_app_settings()
    detail = next(d for status, check, d in results if check == "settings_load")
    assert "must-not-be-printed" not in detail
    assert "RuntimeError" in detail


def test_exit_codes(monkeypatch, capsys):
    monkeypatch.setattr(pf, "run", lambda _db: [(pf.PASS, "a", ""), (pf.WARN, "b", "")])
    assert pf.main([]) == 0
    assert pf.main(["--strict"]) == 2
    monkeypatch.setattr(pf, "run", lambda _db: [(pf.FAIL, "a", ""), (pf.WARN, "b", "")])
    assert pf.main([]) == 1
    capsys.readouterr()


def test_release_identity_is_enforced_outside_development_only():
    staging = dict(environment="staging", admin_supabase_user_ids="u1")
    assert _status(_run(_settings(**staging)), "release_identity") == [pf.PASS]
    for bad in (dict(release_sha=""), dict(release_version="9.9.9"), dict(release_version=""),
                dict(image_digest="sha256:short"), dict(image_digest="")):
        assert _status(_run(_settings(**staging, **bad)), "release_identity") == [pf.FAIL], bad
    assert _status(_run(_settings(release_sha="", image_digest="")), "release_identity") == []


def test_disabled_readyz_enforcement_warns_outside_development():
    staging = dict(environment="staging", admin_supabase_user_ids="u1")
    assert _status(_run(_settings(**staging)), "readyz_enforcement") == []
    assert _status(_run(_settings(**staging, readyz_fail_when_not_ready=False)), "readyz_enforcement") == [pf.WARN]
    assert _status(_run(_settings(readyz_fail_when_not_ready=False)), "readyz_enforcement") == []


def test_database_migration_and_fencing_checks_normalize_plain_postgresql_url():
    source = (ROOT / "scripts" / "paper_preflight.py").read_text(encoding="utf-8")
    assert source.count("create_async_engine(_async_engine_url(url))") == 2
