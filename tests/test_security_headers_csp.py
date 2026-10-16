from pathlib import Path


def _src() -> str:
    return Path("app/monitoring.py").read_text(encoding="utf-8")


def _policy(src: str, name: str) -> str:
    start = src.index(f"{name} = (")
    end = src.index("\n)", start)
    return src[start:end]


def test_admin_csp_blocks_inline_scripts_and_allows_same_origin_script_and_qr_image():
    src = _src()
    policy = _policy(src, "CONTENT_SECURITY_POLICY")
    assert "img-src 'self' data:" in policy
    assert "script-src 'self';" in policy
    assert "script-src 'self' 'unsafe-inline'" not in policy
    for forbidden in ("http://", "https://", "*", "'unsafe-eval'"):
        assert forbidden not in policy


def test_legacy_customer_policy_is_separate_until_customer_inline_handlers_are_migrated():
    src = _src()
    policy = _policy(src, "COMPAT_CONTENT_SECURITY_POLICY")
    assert "script-src 'self' 'unsafe-inline'" in policy
    assert 'policy = CONTENT_SECURITY_POLICY if path in {"/admin", "/admin/"} else COMPAT_CONTENT_SECURITY_POLICY' in src


def test_csp_hardening_directives_present_in_both_policies():
    src = _src()
    for directive in ("object-src 'none'", "base-uri 'self'", "frame-ancestors 'none'", "form-action 'self'"):
        assert src.count(directive) >= 2


def test_csp_is_sent_in_staging_and_production_but_hsts_remains_production_only():
    src = _src()
    assert 'str(settings.environment).lower() in {"staging", "production"}' in src
    assert 'response.headers["Content-Security-Policy"] = policy' in src
    assert src.index('str(settings.environment).lower() == "production"') < src.index("Strict-Transport-Security") < src.index('.lower() in {"staging", "production"}')
