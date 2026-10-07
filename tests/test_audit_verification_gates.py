from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_customer_binance_secret_manager_dependency_is_declared():
    req = (ROOT / "requirements.txt").read_text()
    source = (ROOT / "app/customer_binance_execution.py").read_text()
    assert "google-cloud-secret-manager>=2.24.0,<3" in req
    assert "from google.cloud import secretmanager" in source
    assert "SecretManagerServiceClient" in source


def test_single_worker_live_gate_is_enforced():
    deploy = (ROOT / "deploy/cloud-run-worker-deploy.sh").read_text()
    config = (ROOT / "app/config.py").read_text()
    main = (ROOT / "app/main.py").read_text()
    assert "--instances 1" in deploy
    assert "PROCESS_ROLE=worker" in deploy
    assert "require_single_worker_for_live: bool = True" in config
    assert "WEB_CONCURRENCY" in main
    assert "Live trading requires exactly one active execution worker" in main


def test_strategy_outcome_closed_state_is_explicit_and_fail_closed():
    db = (ROOT / "app/db.py").read_text()
    execution = (ROOT / "app/execution.py").read_text()
    main = (ROOT / "app/main.py").read_text()
    migration = (ROOT / "alembic/versions/0046_strategy_outcome_lifecycle.py").read_text()
    assert "lifecycle_status: Mapped[str]" in db
    assert 'lifecycle_status="OPEN"' in execution
    assert 'outcome.lifecycle_status = "CLOSED"' in execution
    assert 'StrategyOutcome.lifecycle_status == "CLOSED"' in main
    assert 'revision = "0046_strategy_outcome_lifecycle"' in migration
    assert 'down_revision = "0045_merge_application_heads"' in migration
    assert "Unknown historical" in migration
