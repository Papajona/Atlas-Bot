from pathlib import Path


def _block(route: str, nxt: str) -> str:
    main = Path("app/main.py").read_text(encoding="utf-8")
    start = main.index(f'@app.post("/api/admin/withdrawals/{{withdrawal_id}}/{route}")')
    return main[start:main.index(f'@app.post("/api/admin/withdrawals/{{withdrawal_id}}/{nxt}")', start)]


def test_reconcile_binds_operator_identity_and_enforces_separation_like_release():
    block = _block("reconcile", "mark-not-sent")

    assert 'authenticated_operator = str(claims.get("sub") or claims.get("user_id") or "").strip()' in block
    assert 'if not authenticated_operator or not _constant_time_text_equal(authenticated_operator, str(req.operator_id or "").strip()):' in block
    assert "release_separation_violation(req.operator_id, x_release_token, [w.first_approved_by, w.second_approved_by])" in block

    # Identity must be bound before opening the DB transaction, and separation
    # checked before querying a provider or mutating the customer ledger.
    assert block.index("authenticated_operator") < block.index("async with SessionLocal()")
    assert block.index("release_separation_violation") < block.index("get_payout_provider")
    assert block.index("release_separation_violation") < block.index("settle_withdrawal")
    assert block.index("release_separation_violation") < block.index("ledger_release_withdrawal")
