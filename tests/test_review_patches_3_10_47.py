"""Regression coverage for the externally reviewed Atlas 3.10.47 hardening patch."""
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
MAIN = (ROOT / "app" / "main.py").read_text(encoding="utf-8")
EXEC = (ROOT / "app" / "execution.py").read_text(encoding="utf-8")


def _b58(addr20_hex: str) -> str:
    bip = pytest.importorskip("bip_utils")
    return bip.Base58Encoder.CheckEncode(b"\x41" + bytes.fromhex(addr20_hex))


SRC_H, DST_H, CON_H = "aa" * 20, "bb" * 20, "cc" * 20


def _log(frm=SRC_H, to=DST_H, amt=1_000_000, contract=CON_H, pad="00" * 12):
    from app.tron_sweep import TRANSFER_TOPIC
    return {
        "address": contract,
        "topics": ["0x" + TRANSFER_TOPIC, pad + frm, pad + to],
        "data": format(amt, "064x"),
    }


def _classify(logs, body=None, tid="ab" * 32, expected=1_000_000, rid=None):
    from app.tron_sweep import classify_solidified_sweep
    receipt = {"id": tid if rid is None else rid, "receipt": {"result": "SUCCESS"}, "log": logs}
    body = body if body is not None else {"txID": tid, "ret": [{"contractRet": "SUCCESS"}]}
    return classify_solidified_sweep(
        tx_body=body, receipt=receipt, transaction_id=tid, source=_b58(SRC_H),
        treasury=_b58(DST_H), contract=CON_H, expected_raw_amount=expected,
    )


def test_sweep_happy_path_still_settles():
    assert _classify([_log()])[0] == "SETTLED"


def test_missing_contract_ret_is_review_not_settled():
    assert _classify([_log()], body={"txID": "ab" * 32})[0] == "REVIEW"


def test_malformed_transaction_or_receipt_shape_is_review():
    from app.tron_sweep import classify_solidified_sweep
    assert classify_solidified_sweep(
        tx_body=["not", "an", "object"], receipt={"id": "ab" * 32},
        transaction_id="ab" * 32, source="TSource", treasury="TTreasury",
        contract=CON_H, expected_raw_amount=1_000_000,
    )[0] == "REVIEW"


def test_missing_or_wrong_receipt_id_is_review():
    assert _classify([_log()], rid="")[0] == "REVIEW"
    assert _classify([_log()], rid="cd" * 32)[0] == "REVIEW"


def test_duplicate_matching_transfers_are_review():
    status, evidence = _classify([_log(), _log()])
    assert status == "REVIEW" and "multiple" in evidence["reason"]


def test_noncanonical_topic_padding_is_not_a_match():
    assert _classify([_log(pad="ff" * 12)])[0] == "REVIEW"


def test_malformed_topic_does_not_raise_or_settle():
    bad = _log()
    bad["topics"][1] = "zz" * 32
    assert _classify([bad])[0] == "REVIEW"


@pytest.mark.parametrize("value", ["nan", "Infinity", "-Infinity", "abc", "-1", "0", None])
def test_usdt_to_raw_rejects_nonfinite_or_invalid_values(value):
    from app.tron_sweep import SweepError, usdt_to_raw
    with pytest.raises(SweepError):
        usdt_to_raw(value)


def test_usdt_to_raw_rejects_more_than_six_decimals():
    from app.tron_sweep import SweepError, usdt_to_raw
    with pytest.raises(SweepError):
        usdt_to_raw("1.0000001")


def test_sweep_nonce_separates_same_amount_sweeps_and_default_is_deterministic():
    from app.tron_sweep import build_sweep_intent
    kw = dict(wallet_id=7, source_address="TSource", amount_usdt="10.25", treasury_address="TTreasury")
    assert build_sweep_intent(**kw).idempotency_key == build_sweep_intent(**kw).idempotency_key
    assert build_sweep_intent(**kw, nonce=0).idempotency_key != build_sweep_intent(**kw, nonce=1).idempotency_key


def test_dsr_fails_closed_when_trial_dispersion_unknown():
    from app.research_validation import deflated_sharpe_report
    import numpy as np
    rng = np.random.default_rng(1)
    report = deflated_sharpe_report(rng.normal(0.001, 0.01, 500), [1.5], n_trials=200, bars_per_year=365)
    assert report["status"] == "INSUFFICIENT_DATA"


def test_admin_claims_uses_role_store_without_allowlist_short_circuit():
    block = MAIN[MAIN.index("async def admin_claims("):MAIN.index("async def auth(")]
    assert "get_roles(uid)" in block
    assert "if uid not in allowed" not in block


def test_admin_login_uses_role_store():
    block = MAIN[MAIN.index("async def admin_auth_login("):MAIN.index('@app.get("/api/admin/auth/mfa/status")')]
    assert "get_roles(" in block
    assert 'claims["sub"] not in allowed' not in block


def test_jwt_requires_exp_iat_sub_and_rejects_anonymous():
    block = MAIN[MAIN.index("async def _supabase_claims("):MAIN.index("async def _customer_mfa_state(")]
    assert '"require": ["exp", "iat", "sub"]' in block
    assert "is_anonymous" in block


def test_jwks_forced_refresh_is_throttled_even_after_initial_failure():
    assert "_JWKS_FORCED_REFRESH_MIN_INTERVAL" in MAIN
    assert '"last_fetch": now' in MAIN
    assert 'if cached_keys:' in MAIN


def test_broadcast_requires_one_outcome_and_protects_terminal_or_txid_sweeps():
    block = MAIN[MAIN.index("async def admin_record_tron_sweep_broadcast("):MAIN.index('@app.post("/api/admin/custody/tron/sweeps/{sweep_id}/reconcile")')]
    assert "already in a terminal state" in block
    assert "if outcome and sweep.transaction_id" in block
    assert "bool(txid) == bool(outcome)" in block


def test_risk_gate_is_finite_and_magnitude_capped():
    assert "math.isfinite(price) and math.isfinite(quantity)" in EXEC
    assert "reducing = opposing_total > 0 and quantity <= opposing_total + 1e-9" in EXEC
    assert 'quantity if side == "buy" else max(0.0, quantity - opposing_qty)' in EXEC


def test_csp_allows_mfa_data_images_and_blocks_framing():
    monitoring = (ROOT / "app" / "monitoring.py").read_text(encoding="utf-8")
    assert "img-src 'self' data:" in monitoring
    assert "frame-ancestors 'none'" in monitoring
    assert "object-src 'none'" in monitoring


def test_deploy_workflow_exports_digest_and_bootstraps_admin():
    workflow = (ROOT / ".github/workflows/production-deploy.yml").read_text(encoding="utf-8")
    deploy = (ROOT / "deploy/cloud-run-deploy.sh").read_text(encoding="utf-8")
    assert "export EXPECTED_IMAGE_DIGEST" in workflow
    assert "ADMIN_ROLE_ASSIGNMENTS" in workflow
    assert "ADMIN_ROLE_ASSIGNMENTS" in deploy
    assert "ADMIN_BOOTSTRAP_FOUND" in deploy
    assert "administrator role assignment user IDs must be UUIDs" in deploy
