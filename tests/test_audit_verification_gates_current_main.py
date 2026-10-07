from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_customer_binance_secret_manager_dependency_is_declared():
    req = (ROOT / "requirements.txt").read_text(encoding="utf-8")
    source = (ROOT / "app/customer_binance_execution.py").read_text(encoding="utf-8")
    assert "google-cloud-secret-manager>=2.24.0,<3" in req
    assert "from google.cloud import secretmanager" in source
    assert "SecretManagerServiceClient" in source


def test_single_worker_live_gate_is_enforced():
    deploy = (ROOT / "deploy/cloud-run-worker-deploy.sh").read_text(encoding="utf-8")
    config = (ROOT / "app/config.py").read_text(encoding="utf-8")
    main = (ROOT / "app/main.py").read_text(encoding="utf-8")
    assert "--instances 1" in deploy
    assert "PROCESS_ROLE=worker" in deploy
    assert "require_single_worker_for_live" in config
    assert "WEB_CONCURRENCY" in main
    assert "Live trading requires exactly one active execution worker" in main


def test_strategy_outcome_closed_state_is_explicit_and_fail_closed():
    db = (ROOT / "app/db.py").read_text(encoding="utf-8")
    execution = (ROOT / "app/execution.py").read_text(encoding="utf-8")
    main = (ROOT / "app/main.py").read_text(encoding="utf-8")
    migration = (ROOT / "alembic/versions/0047_strategy_outcome_lifecycle.py").read_text(encoding="utf-8")
    assert "lifecycle_status: Mapped[str]" in db
    assert 'lifecycle_status="OPEN"' in db
    assert 'lifecycle_status="OPEN"' in migration
    assert 'outcome.lifecycle_status = "CLOSED"' in execution
    assert 'StrategyOutcome.lifecycle_status == "CLOSED"' in main
    assert 'revision = "0047_strategy_outcome_lifecycle"' in migration
    assert 'down_revision = "0046_subscription_ai_usage"' in migration
    assert "Unknown historical" in migration


def test_normal_position_exit_closes_strategy_outcomes():
    execution = (ROOT / "app/execution.py").read_text(encoding="utf-8")
    assert 'lifecycle_status="CLOSED" if abs(float(position.quantity or 0.0)) < 1e-12 else "OPEN"' in execution
    assert 'if abs(float(position.quantity or 0.0)) < 1e-12:' in execution
    assert 'outcome.lifecycle_status = "CLOSED"' in execution
