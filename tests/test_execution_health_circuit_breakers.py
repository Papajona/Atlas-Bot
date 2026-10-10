from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_execution_health_breakers_are_configured_and_fail_closed():
    config = (ROOT / "app" / "config.py").read_text(encoding="utf-8")
    execution = (ROOT / "app" / "execution.py").read_text(encoding="utf-8")
    assert "max_consecutive_losses: int = 3" in config
    assert "max_order_latency_ms_p95: int = 2_000" in config
    assert "max_order_error_rate_5m: float = 0.05" in config
    assert "_execution_health_block" in execution
    assert "Execution-health halt:" in execution
    assert 'severity="CRITICAL"' in execution


def test_continuous_risk_enforcement_runs_in_worker():
    main = (ROOT / "app" / "main.py").read_text(encoding="utf-8")
    assert "async def _continuous_risk_enforcement_loop():" in main
    assert "state.kill_switch = True" in main
    assert "state.live_enabled = False" in main
    assert 'state.mode = "HALTED"' in main
    assert "await emergency_stop()" in main
    assert '_track_supervised_worker("continuous-risk-enforcement", _continuous_risk_enforcement_loop)' in main
