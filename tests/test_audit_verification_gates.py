from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_execution_dependency_is_declared():
    requirements = (ROOT / "requirements.txt").read_text()
    assert "google-cloud-secret-manager>=2.24.0,<3" in requirements

    source = (ROOT / "app/customer_binance_execution.py").read_text()
    assert "from google.cloud import secretmanager" in source
    assert "SecretManagerServiceClient" in source


def test_worker_deployment_enforces_single_instance():
    script = (ROOT / "deploy/cloud-run-worker-deploy.sh").read_text()
    assert "--instances 1" in script
    assert "PROCESS_ROLE=worker" in script

    config = (ROOT / "app/config.py").read_text()
    assert "require_single_worker_for_live: bool = True" in config

    main = (ROOT / "app/main.py").read_text()
    assert 'settings.require_single_worker_for_live' in main
    assert 'WEB_CONCURRENCY' in main
    assert "Live trading requires exactly one active execution worker" in main


def test_strategy_outcomes_have_explicit_closed_state():
    db = (ROOT / "app/db.py").read_text()
    execution = (ROOT / "app/execution.py").read_text()
    main = (ROOT / "app/main.py").read_text()
    migration = (ROOT / "alembic/versions/0046_strategy_outcome_lifecycle.py").read_text()

    assert 'lifecycle_status: Mapped[str]' in db
    assert 'lifecycle_status="OPEN"' in execution
    assert 'outcome.lifecycle_status = "CLOSED"' in execution
    assert 'StrategyOutcome.lifecycle_status == "CLOSED"' in main
    assert 'revision = "0046_strategy_outcome_lifecycle"' in migration
    assert 'down_revision = "0045_merge_application_heads"' in migration
    assert "fail closed" in migration


def test_strategy_router_contract_rejects_partial_outcomes():
    source = (ROOT / "app/main.py").read_text()
    start = source.index("select(StrategyOutcome)")
    end = source.index(").scalars().all()", start)
    block = source[start:end]
    assert 'StrategyOutcome.lifecycle_status == "CLOSED"' in block
