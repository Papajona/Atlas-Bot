from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
MAIN_ACTIVITY = (
    ROOT / "app" / "src" / "main" / "java" / "com" / "atlas" / "trading" / "MainActivity.kt"
).read_text(encoding="utf-8")
RISK_DASHBOARD = (
    ROOT / "app" / "src" / "main" / "java" / "com" / "atlas" / "trading" / "ui" / "RiskDashboard.kt"
).read_text(encoding="utf-8")


def test_customer_paper_operator_audit_displays_configured_native_ai_safety_budget():
    assert "RiskDashboardScreen" in MAIN_ACTIVITY
    audit_dialog = RISK_DASHBOARD.split("if (showAuditDialog)", 1)[1].split(
        "@Composable\nprivate fun SystemStateRibbon", 1
    )[0]
    assert "AlertDialog(" in audit_dialog
    assert "AI Safety Decision Budget: $aiSafetyDeadlineLabel configured" in audit_dialog
    assert "Native AI veto reserve protected" in audit_dialog
    assert "AI Safety Decision Deadline" not in audit_dialog
    assert "1.5s FAIL-CLOSED" not in audit_dialog
