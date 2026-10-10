"""Structural guards for withdrawal payout error handling in app/main.py."""
import ast
import pathlib

ROOT = pathlib.Path(__file__).resolve().parents[1]
MAIN = ast.parse((ROOT / "app" / "main.py").read_text(encoding="utf-8"))
PAYOUT = ast.parse((ROOT / "app" / "payout.py").read_text(encoding="utf-8"))


def _fn(name):
    return next(n for n in ast.walk(MAIN) if isinstance(n, (ast.AsyncFunctionDef, ast.FunctionDef)) and n.name == name)


def _payout_try_blocks(fn):
    out = []
    for node in ast.walk(fn):
        if isinstance(node, ast.Try):
            names = [ast.unparse(h.type) if h.type is not None else "bare" for h in node.handlers]
            if any("Payout" in n for n in names):
                out.append((node, names))
    return out


def test_payout_unknown_is_a_subclass_of_payout_error():
    bases = {n.name: [ast.unparse(b) for b in n.bases] for n in PAYOUT.body if isinstance(n, ast.ClassDef)}
    assert bases["PayoutUnknown"] == ["PayoutError"]


def test_release_withdrawal_handler_order_keeps_failed_branch_reachable():
    send_try = [names for _, names in _payout_try_blocks(_fn("release_withdrawal")) if "PayoutUnknown" in names]
    assert len(send_try) == 1, "the provider.send try block must handle PayoutUnknown explicitly"
    names = send_try[0]
    assert names.index("PayoutUnknown") < names.index("PayoutError"), names
    assert len(names) == len(set(names)), f"duplicate except clause makes a branch unreachable: {names}"


def test_release_withdrawal_only_releases_the_reserve_in_the_definite_failure_branch():
    node, names = next((n, nm) for n, nm in _payout_try_blocks(_fn("release_withdrawal")) if "PayoutUnknown" in nm)
    unknown_h = node.handlers[names.index("PayoutUnknown")]
    failed_h = node.handlers[names.index("PayoutError")]
    assert "ledger_release_withdrawal" not in ast.unparse(unknown_h)
    assert "ledger_release_withdrawal" in ast.unparse(failed_h)


def test_reconcile_catches_every_payout_error_not_only_unknown():
    blocks = _payout_try_blocks(_fn("reconcile_withdrawal"))
    assert blocks and all("PayoutError" in names for _, names in blocks), [n for _, n in blocks]


def test_mark_not_sent_requires_dual_control_and_unknown_status_and_evidence():
    fn = _fn("mark_withdrawal_not_sent")
    src = ast.unparse(fn)
    for needle in ("require_role(claims, 'TREASURY')", "approver_auth(", "verify_release_operator(",
                   "w.status == 'SUBMITTING'", "w.status != 'UNKNOWN'", "w.provider_id", "ledger_release_withdrawal(",
                   "_withdrawal_release_ref("):
        assert needle in src, needle
    assert "authorization" in [a.arg for a in fn.args.args]
    assert "recover(" in src and "use reconcile instead" in src
    schemas = ast.parse((ROOT / "app" / "schemas.py").read_text(encoding="utf-8"))
    cls = next(n for n in schemas.body if isinstance(n, ast.ClassDef) and n.name == "WithdrawalNotSentRequest")
    assert "min_length=20" in ast.unparse(cls), "evidence must have a minimum length"


def test_immediate_provider_failure_releases_the_withdrawal_reserve():
    fn = _fn("release_withdrawal")
    text = ast.unparse(fn)
    assert "w3.status = 'FAILED'" in text
    assert "result.status == 'FAILED'" in text
    assert 'ledger_release_withdrawal(db3, w3.customer_id, w3.amount' in text


def test_immediate_provider_unknown_never_releases_reserve():
    fn = _fn("release_withdrawal")
    text = ast.unparse(fn)
    unknown = next(n for n in ast.walk(fn)
                   if isinstance(n, ast.ExceptHandler)
                   and ast.unparse(n.type) == "PayoutUnknown")
    assert "ledger_release_withdrawal" not in ast.unparse(unknown)
    assert "_mark_withdrawal_unknown(" in ast.unparse(unknown)
    helper = _fn("_mark_withdrawal_unknown")
    assert "w.status = 'UNKNOWN'" in ast.unparse(helper)
    assert "ledger_release_withdrawal" not in ast.unparse(helper)


def test_withdrawal_provider_identifier_is_unique_and_recovery_is_durable():
    db = (ROOT / "app" / "db.py").read_text(encoding="utf-8")
    migration = (ROOT / "alembic" / "versions" / "0034_withdrawal_provider_identity.py").read_text(encoding="utf-8")
    assert "uq_withdrawal_provider_id_nonempty" in db
    assert "unique=True" in db
    assert "provider_id <> ''" in migration
    main = (ROOT / "app" / "main.py").read_text(encoding="utf-8")
    loop_start = main.index("async def _withdrawal_recovery_loop")
    loop_end = main.index("\ndef _tron_base58check_valid", loop_start)
    recovery_loop = main[loop_start:loop_end]
    assert 'Withdrawal.status.in_(["UNKNOWN", "SUBMITTED"])' in recovery_loop
    assert "WITHDRAWAL_RECOVERY_DUE" in recovery_loop
    assert "no blind payout retry" in recovery_loop


def test_withdrawal_reconciliation_never_turns_unresolved_provider_state_into_success():
    fn = _fn("reconcile_withdrawal")
    text = ast.unparse(fn)
    assert "PayoutUnknown" in text
    assert "raise _safe_http_error" in text
    assert "result.status == 'COMPLETED'" in text
    assert "result.status == 'FAILED'" in text
    assert "w.status = 'RELEASED'" in text
    assert "'FAILED' if result.status == 'FAILED'" in text


def test_final_money_safety_has_no_direct_blind_retry_after_restart():
    main = (ROOT / "app" / "main.py").read_text(encoding="utf-8")
    loop_start = main.index("async def _withdrawal_recovery_loop")
    loop_end = main.index("\ndef _tron_base58check_valid", loop_start)
    recovery_loop = main[loop_start:loop_end]
    assert ".send(" not in recovery_loop
    assert "provider.send" not in recovery_loop
    assert "reconcile" not in recovery_loop.lower() or "no blind payout retry" in recovery_loop


def test_sweep_and_withdrawal_terminal_states_are_explicit():
    custody = (ROOT / "app" / "custody_locations.py").read_text(encoding="utf-8")
    assert '"CONFIRMED"' in custody and '"FAILED"' in custody and '"RECONCILIATION_REQUIRED"' in custody
    assert '"UNKNOWN"' in custody
    sweep = (ROOT / "app" / "tron_sweep.py").read_text(encoding="utf-8")
    assert 'return "UNKNOWN"' in sweep
