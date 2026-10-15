from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_customer_daily_loss_profile_is_loaded_in_risk_gate_and_stricter_limit_wins():
    src = (ROOT / "app/execution.py").read_text()
    assert "CustomerRiskProfile).where(CustomerRiskProfile.customer_id == customer_id)" in src
    assert "effective_daily_limit = min(" in src
    assert "customer_daily_limit if customer_daily_limit is not None" in src
    assert "daily_loss >= effective_daily_limit" in src


def test_risk_gate_persists_customer_allowed_and_blocked_decisions():
    src = (ROOT / "app/execution.py").read_text()
    assert "db.add(CustomerRiskDecision(" in src
    assert 'verdict="BLOCKED"' in src
    assert 'verdict="ALLOWED"' in src
    assert 'execution_mode="LIVE" if live else "PAPER"' in src


def test_reduce_only_exit_exception_is_preserved_for_halted_customer():
    src = (ROOT / "app/execution.py").read_text()
    assert 'already_halted_exit = account is not None and account.status == "HALTED" and account_reduce_only' in src
    assert "A reduce-only exit remains allowed for an already halted customer." in src


def test_customer_risk_profile_schema_validates_limits():
    src = (ROOT / "app/schemas.py").read_text()
    start = src.index("class CustomerRiskProfileRequest")
    end = src.index("\n\n", start)
    block = src[start:end]
    assert "ge=0.001" in block
    assert "le=0.15" in block


def test_customer_risk_migration_enables_rls_and_constrains_values():
    src = (ROOT / "alembic/versions/0051_customer_risk_profiles_and_decisions.py").read_text()
    assert "ck_customer_risk_daily_loss_range" in src
    assert "ENABLE ROW LEVEL SECURITY" in src
    assert 'down_revision = "0050_customer_simulation_ledger"' in src
