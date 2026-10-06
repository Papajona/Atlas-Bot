from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_customer_pilot_fields_and_migration_are_fail_closed():
    db = (ROOT / "app/db.py").read_text()
    mig = (ROOT / "alembic/versions/0044_customer_pilot_risk_controls.py").read_text()
    assert 'pilot_status: Mapped[str] = mapped_column(String(20), default="NONE")' in db
    assert 'pilot_max_position_notional_usd' in db
    assert 'pilot_max_total_exposure_usd' in db
    assert 'pilot_max_open_positions' in db
    assert 'pilot_max_leverage' in db
    assert 'pilot_daily_loss_limit' in db
    assert 'revision = "0044_customer_pilot_risk_controls"' in mig
    assert 'down_revision = "0043_live_trading_dual_control"' in mig
    assert "pilot_status='NONE'" in mig


def test_live_execution_requires_customer_pilot_approval_and_expiry():
    src = (ROOT / "app/live_execution.py").read_text()
    assert 'pilot_status = str(account.pilot_status or "NONE").upper()' in src
    assert 'if pilot_status != "APPROVED":' in src
    assert 'if pilot_expires_at is None:' in src
    assert 'if pilot_expires_at <= datetime.now(timezone.utc):' in src


def test_risk_gate_enforces_customer_pilot_caps_for_live_orders():
    src = (ROOT / "app/execution.py").read_text()
    assert 'if live and customer_id is not None:' in src
    assert 'Customer is not approved for the live pilot' in src
    assert 'pilot_max_notional = float(account.pilot_max_position_notional_usd or 0)' in src
    assert 'pilot_max_exposure = float(account.pilot_max_total_exposure_usd or 0)' in src
    assert 'pilot_max_positions = int(account.pilot_max_open_positions or 0)' in src
    assert 'pilot_max_leverage = float(account.pilot_max_leverage or 0)' in src
    assert 'pilot_daily_loss = float(account.pilot_daily_loss_limit or 0)' in src
    assert 'max_notional_limit = min(max_notional_limit, pilot_max_notional)' in src
    assert 'max_exposure_limit = min(max_exposure_limit, pilot_max_exposure)' in src
    assert 'max_open_position_limit = min(settings.max_open_positions, pilot_max_positions)' in src


def test_customer_pilot_endpoint_requires_dual_risk_officer_approval():
    src = (ROOT / "app/main.py").read_text()
    start = src.index('@app.post("/api/admin/customers/{customer_id}/pilot")')
    end = src.index('@app.post("/api/live/enable")', start)
    block = src[start:end]
    assert 'await require_role(claims, "RISK_OFFICER")' in block
    assert 'account.pilot_status = "PENDING"' in block
    assert 'A different RISK_OFFICER must approve the customer pilot' in block
    assert 'account.pilot_status = "APPROVED"' in block
    assert 'account.pilot_status = "SUSPENDED"' in block
