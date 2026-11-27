from pathlib import Path


def test_android_sign_out_clears_local_session_before_remote_revocation():
    source = Path(
        "app/src/main/java/com/atlas/trading/ui/UserDashboardScreen.kt"
    ).read_text(encoding="utf-8")
    start = source.index("onSignOut = {")
    end = source.index("modifier = Modifier.padding(innerPadding)", start)
    handler = source[start:end]

    local_clear = handler.index("activeCustomerToken = null")
    remote_request = handler.index("CustomerAuthClient(backendUrl).logout(token)")
    assert local_clear < remote_request
    assert handler.index("customerSnapshot = null") < remote_request
    assert "if (activeCustomerToken == null)" in handler
