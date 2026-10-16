"""Static regressions for the admin withdrawal UI and operator controls."""
from pathlib import Path


def _html() -> str:
    return Path("app/templates/dashboard.html").read_text(encoding="utf-8")


def _js() -> str:
    return Path("app/static/admin-dashboard.js").read_text(encoding="utf-8")


def test_approver_secret_is_not_persisted_or_attached_to_general_api_requests():
    js = _js()
    assert "setItem('ait_approver_token'" not in js
    assert "setItem('ait_admin_refresh_token'" not in js
    assert "removeItem('ait_approver_token')" in js
    headers = js[js.index("function headers()"):js.index("async function api(")]
    assert "X-Approver-Token" not in headers
    approve = js[js.index("async function approveWithdrawal"):js.index("async function executeWithdrawal")]
    reject = js[js.index("async function rejectWithdrawal"):js.index("async function loadRoles")]
    assert "X-Approver-Token" in approve
    assert "X-Approver-Token" in reject


def test_execute_confirmation_contains_amount_destination_and_proposal_digest():
    js = _js()
    block = js[js.index("async function executeWithdrawal"):js.index("async function prepareWithdrawalProposal")]
    assert "if(!confirm(" in block
    assert "Release payout #" in block
    assert "wr.amount" in block
    assert "wr.destination_masked" in block
    assert "Proposal SHA-256: '+wr.proposal_digest" in block
    assert "A valid SHA-256 proposal digest is required" in block


def test_withdrawal_amount_precision_and_missing_risk_score_are_explicit():
    js = _js()
    assert "maximumFractionDigits:6" in js
    assert "x.risk_score==null?'n/a':Number(x.risk_score).toFixed(2)" in js


def test_poll_failures_do_not_overwrite_operator_result_panel():
    js = _js()
    block = js[js.index("async function loadWithdrawals"):js.index("async function approveWithdrawal")]
    assert "$('wdError').textContent='Withdrawal queue: '+e.message" in block
    assert "$('output').textContent='Withdrawal queue: '+e.message" not in block


def test_polling_waits_for_mfa_verified_session_and_identity_is_token_bound():
    js = _js()
    assert "function actorId()" in js
    assert "$('adminId').value=id;$('releaseId').value=id" in js
    assert "ait_admin_mfa_verified')==='true'" in js
    assert "setInterval(()=>{if(loggedIn())refresh()},5000)" in js
    assert "setInterval(()=>{if(loggedIn())loadWithdrawals()},10000)" in js
    assert "function adminLogout()" in js
    assert "$('adminLogoutBtn').hidden=true" in js


def test_proposal_digest_is_shown_near_execute_and_missing_digest_cannot_execute():
    js = _js()
    assert "x.proposal_digest||'missing — prepare before execute'" in js
    assert "data-action=\"prepare-proposal\"" in js
    assert "data-action=\"execute-withdrawal\"" in js
    assert "A valid SHA-256 proposal digest is required" in js


def test_dashboard_has_no_inline_script_or_event_handler():
    html = _html()
    assert '<script src="/static/admin-dashboard.js" defer></script>' in html
    assert "<script>" not in html
    assert "onclick=" not in html
    assert "onchange=" not in html
    js = _js()
    assert "document.addEventListener('click'" in js
    assert "document.addEventListener('change'" in js
