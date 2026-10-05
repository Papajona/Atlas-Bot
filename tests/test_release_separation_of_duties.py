import pytest

from app.config import settings
from app.withdrawal_security import release_separation_violation


@pytest.fixture(autouse=True)
def tokens(monkeypatch):
    monkeypatch.setattr(settings, "withdrawal_approver_tokens", "alice:tok-alice,bob:tok-bob")
    monkeypatch.setattr(settings, "withdrawal_release_tokens", "carol:tok-carol,alice:tok-alice-release")


def test_distinct_release_operator_is_allowed():
    assert release_separation_violation("carol", "tok-carol", ["alice", "bob"]) is None


@pytest.mark.parametrize("operator", ["alice", "BOB", " alice "])
def test_release_operator_who_approved_is_refused(operator):
    # 'alice' is also configured as a release operator in this fixture: the config alone must not allow it.
    assert "different from the withdrawal approvers" in release_separation_violation(operator, "tok-alice-release", ["alice", "bob"])


def test_release_token_reused_from_an_approver_is_refused():
    assert "distinct from every approver credential" in release_separation_violation("carol", "tok-bob", ["alice", "bob"])


def test_missing_approver_names_do_not_crash_or_block():
    assert release_separation_violation("carol", "tok-carol", [None, ""]) is None


def test_release_handler_enforces_the_check_before_marking_submitting():
    src = open("app/main.py").read()
    handler = src[src.index("async def release_withdrawal("):]
    handler = handler[:handler.index("\n@app.", 10)]
    assert "release_separation_violation(req.operator_id, x_release_token," in handler
    assert handler.index("release_separation_violation(") < handler.index('w.status = "SUBMITTING"')
