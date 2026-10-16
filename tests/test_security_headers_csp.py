from pathlib import Path


def _src() -> str:
    return Path("app/monitoring.py").read_text(encoding="utf-8")


def _policy(src: str) -> str:
    start = src.index("CONTENT_SECURITY_POLICY = (")
    end = src.index("\n)", start)
    return src[start:end]


def test_csp_allows_svg_qr_image_and_blocks_inline_script_execution():
    policy = _policy(_src())
    assert "img-src 'self' data:" in policy
    assert "script-src 'self';" in policy
    assert "script-src 'self' 'unsafe-inline'" not in policy
    for forbidden in ("http://", "https://", "*", "'unsafe-eval'"):
        assert forbidden not in policy


def test_csp_hardening_directives_present():
    src = _src()
    policy = _policy(src)
    for directive in ("object-src 'none'", "base-uri 'self'", "frame-ancestors 'none'", "form-action 'self'"):
        assert directive in policy


def test_same_strict_csp_is_sent_in_staging_and_production_but_hsts_remains_production_only():
    src = _src()
    assert 'str(settings.environment).lower() in {"staging", "production"}' in src
    assert 'response.headers["Content-Security-Policy"] = CONTENT_SECURITY_POLICY' in src
    assert src.index('str(settings.environment).lower() == "production"') < src.index("Strict-Transport-Security") < src.index('.lower() in {"staging", "production"}')
    assert "COMPAT_CONTENT_SECURITY_POLICY" not in src
