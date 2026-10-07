from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_manual_funding_review_is_enabled_by_default():
    config = (ROOT / "app" / "config.py").read_text()
    assert "funding_manual_review_required: bool = True" in config


def test_tron_confirmed_deposit_stops_before_ledger_credit_when_review_required():
    source = (ROOT / "app" / "main.py").read_text()
    scanner = source[source.index("async def _usdt_tron_monitor_loop"):source.index("async def _reconciliation_loop")]
    assert 'status=("PENDING_REVIEW" if settings.funding_manual_review_required else "CONFIRMED")' in scanner
    assert "if not settings.funding_manual_review_required:" in scanner
    assert "await post_deposit(" in scanner


def test_admin_funding_approval_is_role_protected_and_credits_ledger():
    source = (ROOT / "app" / "main.py").read_text()
    block = source[source.index('@app.post("/api/admin/funding/{funding_id}/approve")'):source.index('@app.post("/api/admin/funding/{funding_id}/reject")')]
    assert 'await require_role(claims, "FINANCE")' in block
    assert 'str(funding.status or "").upper() != "PENDING_REVIEW"' in block
    assert "await post_deposit(" in block
    assert '"FUNDING_APPROVED"' in block


def test_admin_rejection_does_not_credit_ledger():
    source = (ROOT / "app" / "main.py").read_text()
    block = source[source.index('@app.post("/api/admin/funding/{funding_id}/reject")'):source.index('@app.get("/api/customer/funding")')]
    assert 'await require_role(claims, "FINANCE")' in block
    assert 'funding.status = "REJECTED"' in block
    assert "await post_deposit(" not in block
