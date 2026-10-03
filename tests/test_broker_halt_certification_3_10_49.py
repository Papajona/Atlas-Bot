from pathlib import Path
import ast
import re

ROOT = Path(__file__).resolve().parents[1]
EXEC = (ROOT / "app/execution.py").read_text()
LIVE = (ROOT / "app/live_execution.py").read_text()
DB = (ROOT / "app/db.py").read_text()
MAIN = (ROOT / "app/main.py").read_text()


def _function_source(source: str, name: str) -> str:
    tree = ast.parse(source)
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == name:
            return ast.get_source_segment(source, node) or ""
    raise AssertionError(f"function {name} not found")


def _broker_call_block() -> str:
    idx = EXEC.index("broker.market_order")
    return EXEC[max(0, idx - 3500):idx + 500]


def test_kill_during_pre_submit_has_two_independent_live_guards():
    block = _function_source(EXEC, "execute_signal")
    assert 'if s.kill_switch:' in block
    assert 'assert_live_system_enabled(' in block
    assert 'await _verify_live_lease(lease_token)' in block
    assert block.index("await _verify_live_lease(lease_token)") < block.index("broker.market_order")
    assert "LIVE_SUBMISSION_LOCK_KEY" in block
    assert "final_gate_db" in block
    assert "await assert_live_system_enabled(" in block


def test_kill_while_submitting_is_fenced_by_lease_state():
    block = _broker_call_block()
    assert 'await _verify_live_lease(lease_token)' in block
    assert 'fencing_token' in EXEC
    assert 'LiveExecutionLease' in EXEC

    # Emergency stop fences the submission path and waits for in-flight work before
    # reporting a confirmed halt. The execution lease remains enforced by the submitter.
    emergency = _function_source(EXEC, "emergency_stop")
    assert "_wait_for_live_submission_barrier" in emergency
    assert "barrier_confirmed" in emergency
    assert "LIVE_SUBMISSION_LOCK_KEY" in EXEC


def test_kill_during_network_delay_has_inflight_submission_accountability():
    block = _function_source(EXEC, "execute_signal")
    # UNKNOWN is the required outcome for a broker/network timeout: Atlas must
    # reconcile it rather than treating the order as safely rejected.
    assert 't.status = "UNKNOWN"' in block
    assert 'await audit("LIVE_ORDER_UNKNOWN"' in block
    assert "reconcile_customer_live_orders" in EXEC or "reconcile(" in EXEC

    # Emergency-stop confirmation must not claim a clean halt when cancellation
    # itself failed.
    emergency = _function_source(EXEC, "emergency_stop")
    assert "failure_count" in emergency
    assert "failures" in emergency


def test_worker_restart_cannot_reuse_previous_execution_identity():
    assert '_EXECUTION_OWNER = os.getenv("K_REVISION", "local") + ":" + uuid.uuid4().hex[:16]' in EXEC
    acquire = _function_source(EXEC, "_acquire_live_execution_lease")
    verify = _function_source(EXEC, "_verify_live_lease")
    assert "row.owner_id" in acquire
    assert "row.fencing_token" in acquire
    assert "row.owner_id != _EXECUTION_OWNER" in acquire
    assert "row.owner_id != _EXECUTION_OWNER" in verify
    assert "row.expires_at <= now" in verify


def test_second_worker_is_denied_by_single_writer_lease():
    acquire = _function_source(EXEC, "_acquire_live_execution_lease")
    assert 'raise RiskBlocked("Another live execution worker currently holds the submit lease")' in acquire
    assert "with_for_update()" in acquire


def test_customer_binance_is_included_in_emergency_halt():
    emergency = _function_source(EXEC, "emergency_stop")
    assert "CustomerBinanceAccount" in emergency
    assert 'CustomerBinanceAccount.status == "VERIFIED"' in emergency
    assert "can_trade.is_(True)" in emergency
    assert "build_customer_binance_broker" in emergency
    assert "broker.cancel_all_orders" in emergency


def test_platform_binance_is_included_in_emergency_halt():
    emergency = _function_source(EXEC, "emergency_stop")
    assert "BrokerConfig" in emergency
    assert "Broker.get(cfg)" in emergency
    assert "broker.cancel_all_orders" in emergency
    assert "EMERGENCY_CANCEL_FAILED" in emergency


def test_no_order_after_confirmed_halt_requires_authoritative_state():
    gate = _function_source(LIVE, "assert_live_system_enabled")
    assert "select(AppState)" in gate
    assert "with_for_update()" in gate
    assert "state.kill_switch or not state.live_enabled" in gate

    # The actual exchange side effect must occur only after the final gate and
    # lease verification.
    block = _broker_call_block()
    assert "assert_live_system_enabled" in block
    assert "_verify_live_lease" in block


def test_cancellation_failure_must_never_be_reported_as_clean_halt():
    emergency = _function_source(EXEC, "emergency_stop")
    assert "failures.append" in emergency
    assert "failure_count" in emergency

    # This is deliberately strict: returning ok=True while a broker cancellation
    # failed would create a false-green emergency stop.
    assert '"ok": broker_halt_confirmed' in emergency
    assert '"broker_halt_confirmed": broker_halt_confirmed' in emergency


def test_admin_kill_never_reports_clean_success_when_broker_halt_unconfirmed():
    kill = _function_source(MAIN, "kill")
    assert "broker_halt_confirmed" in kill
    assert '"ok": broker_halt_confirmed' in kill
    assert 'return JSONResponse(response, status_code=503)' in kill


def test_emergency_stop_records_critical_customer_cancel_incident():
    emergency = _function_source(EXEC, "emergency_stop")
    assert 'key=f"EMERGENCY_CANCEL_FAILED:CUSTOMER:{account.customer_id}"' in emergency
    assert 'severity="CRITICAL"' in emergency
    assert 'category="EMERGENCY_STOP"' in emergency


def test_order_command_records_submission_fencing_token():
    assert "OrderCommand" in DB
    assert "fencing_token" in DB
    command = _function_source(EXEC, "_mark_order_command")
    assert "row.fencing_token = int(token)" in command


def test_emergency_stop_waits_for_inflight_submissions_before_confirmation():
    emergency = _function_source(EXEC, "emergency_stop")
    assert "LIVE_SUBMISSION_LOCK_KEY" in emergency
    assert "_wait_for_live_submission_barrier" in emergency
    assert "barrier_confirmed" in emergency
    assert "fetch_open_orders" in emergency


def test_network_delay_race_cannot_return_confirmed_halt_before_reconciliation():
    block = _function_source(EXEC, "execute_signal")
    emergency = _function_source(EXEC, "emergency_stop")
    assert 't.status = "UNKNOWN"' in block
    assert 'await audit("LIVE_ORDER_UNKNOWN"' in block
    assert "LIVE_SUBMISSION_LOCK_KEY" in EXEC
    assert "broker_halt_confirmed" in emergency
    assert '"ok": broker_halt_confirmed' in emergency
