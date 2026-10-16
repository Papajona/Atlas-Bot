"""Static regressions for sensitive withdrawal operator controls."""
from pathlib import Path


def _html() -> str:
    return Path("app/templates/dashboard.html").read_text(encoding="utf-8")


def test_approver_secret_is_not_persisted_or_attached_to_general_api_requests():
    html = _html()
    assert "setItem('ait_approver_token'" not in html
    assert "setItem('ait_admin_refresh_token'" not in html
    assert "removeItem('ait_approver_token')" in html
    headers = html[html.index("function headers()"):html.index("async function api(")]
    assert "X-Approver-Token" not in headers
    approve = html[html.index("async function approveWithdrawal"):html.index("async function executeWithdrawal")]
    reject = html[html.index("async function rejectWithdrawal"):html.index("async function loadRoles")]
    assert "X-Approver-Token" in approve
    assert "X-Approver-Token" in reject


def test_execute_requires_confirmation_with_request_amount_and_destination():
    html = _html()
    block = html[html.index("async function executeWithdrawal"):html.index("async function reconcileWithdrawal")]
    assert "if(!confirm(" in block
    assert "Release payout #" in block
    assert "wr.amount" in block
    assert "wr.destination_masked" in block


def test_withdrawal_amount_uses_six_fractional_digits_and_null_risk_is_not_zero():
    html = _html()
    assert "maximumFractionDigits:6" in html
    assert "x.risk_score==null?'n/a':Number(x.risk_score).toFixed(2)" in html


def test_poll_failures_do_not_overwrite_operator_result_panel():
    html = _html()
    block = html[html.index("async function loadWithdrawals"):html.index("async function approveWithdrawal")]
    assert "$('wdError').textContent='Withdrawal queue: '+e.message" in block
    assert "$('output').textContent='Withdrawal queue: '+e.message" not in block


def test_polling_is_conditional_on_a_session_and_422_detail_is_readable():
    html = _html()
    assert "setInterval(()=>{if(loggedIn())refresh()},5000)" in html
    assert "setInterval(()=>{if(loggedIn())loadWithdrawals()},10000)" in html
    assert "function detailText(d)" in html
    assert "throw new Error(detailText(j.detail)||text||'Request failed')" in html
    assert "new Error(detailText(j.detail)||detailText(j.message)||'Administrator login failed')" in html


def test_untrusted_row_ids_are_coerced_to_numbers_for_inline_handlers():
    html = _html()
    block = html[html.index("async function loadWithdrawals"):html.index("async function approveWithdrawal")]
    assert "approveWithdrawal(\u0024{Number(x.id)})" in block
    assert "rejectWithdrawal(\u0024{Number(x.id)})" in block
    assert "executeWithdrawal(\u0024{Number(x.id)})" in block
    assert "reconcileWithdrawal(\u0024{Number(x.id)})" in block
