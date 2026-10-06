from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
POLICY = ROOT / "docs" / "FOUR_WEEK_FORWARD_VALIDATION.md"


def test_four_week_policy_is_an_interim_gate():
    text = POLICY.read_text()
    assert "28 consecutive calendar days" in text
    assert "does not replace the stronger 90-day evidence requirement" in text
    assert "PAPER/SHADOW only" in text
    assert ">= 0.95" in text
    assert "2x cost-stress" in text


def test_four_week_policy_cannot_be_misrepresented_as_ninety_days():
    text = POLICY.read_text()
    assert "must not be represented as a 90-day pass" in text
    assert ">=90 days" in text
