from pathlib import Path

def test_version_and_distributed_security_controls():
    cfg = Path("app/config.py").read_text()
    main = Path("app/main.py").read_text()
    req = Path("requirements.txt").read_text()
    assert 'app_version: str = "3.10.47"' in cfg
    assert 'allow_rate_limit' in main
    assert 'check_redis' in main
    assert 'ServiceHeartbeat' in main
    assert 'redis==' in req

def test_withdrawal_risk_engine_and_explicit_review():
    risk = Path("app/withdrawal_risk.py").read_text()
    main = Path("app/main.py").read_text()
    assert 'NEW_DESTINATION' in risk
    assert '24H_VELOCITY_LIMIT' in risk
    assert 'WITHDRAWAL_RISK_REVIEWED' in main
    assert 'risk_reviewed_by' in main
    assert 'explicit risk review before release' in main

def test_recovery_is_status_only_and_no_blind_retry():
    main = Path("app/main.py").read_text()
    assert 'WITHDRAWAL_RECOVERY_DUE' in main
    assert 'no blind payout retry' in main

def test_production_compose_has_redis():
    compose = Path("docker-compose.production.yml").read_text()
    assert 'redis:8-alpine' in compose
    assert 'REDIS_URL: redis://redis:6379/0' in compose


def test_binance_custody_gate_is_fail_closed_by_default():
    cfg = Path("app/config.py").read_text()
    assert "binance_custody_reconciliation_enabled: bool = True" in cfg
    custody = Path("app/custody_reconciliation.py").read_text()
    assert 'if settings.binance_custody_reconciliation_enabled:' in custody
    assert '"status": "UNKNOWN"' in custody
    assert '"No fresh Binance custody observation is available; exchange assets are not assumed to be zero"' in custody


def test_withdrawal_recovery_persists_incident():
    main = Path("app/main.py").read_text()
    start = main.index("async def _withdrawal_recovery_loop")
    end = main.index("def _tron_base58check_valid", start)
    block = main[start:end]
    assert 'WITHDRAWAL_RECOVERY_DUE:{w.id}' in block
    assert 'category="WITHDRAWAL_RECOVERY"' in block
    assert 'await incident_db.commit()' in block


def test_risk_reset_blocks_unresolved_financial_safety_incidents():
    main = Path("app/main.py").read_text()
    start = main.index('@app.post("/api/risk/reset")')
    end = main.index('async def _audit', start)
    block = main[start:end]
    assert 'Incident.resolved_at.is_(None)' in block
    assert 'Incident.severity.in_(["CRITICAL", "HIGH"])' in block
    assert '"WITHDRAWAL_RECOVERY"' in block
    assert '"EXECUTION_PROTECTION"' in block
    assert 'Risk reset is blocked while critical financial-safety incidents remain unresolved' in block
    assert block.index("unresolved =") < block.index("s.kill_switch = False")
