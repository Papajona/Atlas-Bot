"""Reject must use the identity of the authenticated approver, not a caller-selected name."""
from pathlib import Path


def test_reject_binds_admin_id_to_authenticated_identity_before_opening_database():
    source = Path("app/main.py").read_text(encoding="utf-8")
    start = source.index('@app.post("/api/admin/withdrawals/{withdrawal_id}/reject")')
    end = source.index('@app.get("/api/admin/withdrawals/summary")', start)
    block = source[start:end]
    assert 'authenticated_approver = str(claims.get("sub") or claims.get("user_id") or "").strip()' in block
    assert 'if not authenticated_approver or not _constant_time_text_equal(authenticated_approver, str(req.admin_id or "").strip()):' in block
    assert 'raise HTTPException(403, "Approver identity must match the authenticated user")' in block
    assert block.index("authenticated_approver") < block.index("async with SessionLocal()")
    assert block.index("approver_auth(req.admin_id") < block.index("async with SessionLocal()")


def test_risk_review_binds_reviewer_to_authenticated_identity_before_opening_database():
    source = Path("app/main.py").read_text(encoding="utf-8")
    start = source.index('@app.post("/api/admin/withdrawals/{withdrawal_id}/risk-review")')
    end = source.index('@app.post("/api/admin/withdrawals/{withdrawal_id}/proposal")', start)
    block = source[start:end]
    assert 'authenticated_reviewer = str(claims.get("sub") or claims.get("user_id") or "").strip()' in block
    assert 'if not authenticated_reviewer or not _constant_time_text_equal(authenticated_reviewer, str(req.admin_id or "").strip()):' in block
    assert 'raise HTTPException(403, "Risk reviewer identity must match the authenticated user")' in block
    assert block.index("authenticated_reviewer") < block.index("async with SessionLocal()")
    assert block.index("approver_auth(req.admin_id") < block.index("async with SessionLocal()")
