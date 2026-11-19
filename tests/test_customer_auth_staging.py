from __future__ import annotations

import asyncio
from pathlib import Path

from app import main


ROOT = Path(__file__).resolve().parents[1]


def test_pending_totp_factor_can_be_challenged_but_phone_factor_cannot(monkeypatch):
    async def fake_supabase_user(token: str):
        assert token == "session-token"
        return {
            "factors": [
                {"id": "totp-pending-12345", "factor_type": "totp", "status": "unverified"},
                {"id": "totp-verified-12345", "factor_type": "totp", "status": "verified"},
                {"id": "phone-pending-12345", "factor_type": "phone", "status": "unverified"},
                {"id": "disabled-totp-12345", "factor_type": "totp", "status": "disabled"},
            ]
        }

    monkeypatch.setattr(main, "_supabase_user", fake_supabase_user)

    factor_ids = asyncio.run(
        main._customer_mfa_factor_ids("Bearer session-token")
    )

    assert factor_ids == {"totp-pending-12345", "totp-verified-12345"}


def test_customer_profile_provision_function_requires_verified_user_totp_aal2_and_confirmed_email():
    source = (
        ROOT / "supabase/functions/customer-profile-provision/index.ts"
    ).read_text(encoding="utf-8")

    assert "withSupabase({ auth: \"user\" }" in source
    assert 'jwtClaims.aal !== "aal2"' in source
    assert 'String(userClaims.id ?? "").trim()' in source
    assert 'String(jwtClaims.sub ?? "").trim()' in source
    assert "ctx.supabase.auth.getUser()" in source
    assert "user.email_confirmed_at" in source
    assert "ctx.supabase.auth.mfa.listFactors()" in source
    assert 'factor.status === "verified"' in source
    assert 'from("customer_profiles")' in source
    assert ".insert({" in source
    assert '"ALREADY_PROVISIONED"' in source
    assert "23505" in source
    assert "email: email.slice" not in source
    assert "display_name: displayName" not in source
    assert "Atlas stores customer_profiles.email and display_name with application-layer" in source
    assert ".update(" not in source
    assert ".upsert(" not in source
    assert "req.json()" not in source


def test_customer_profile_provision_uses_only_authenticated_identity_and_never_rewrites_legacy_profiles():
    source = (
        ROOT / "supabase/functions/customer-profile-provision/index.ts"
    ).read_text(encoding="utf-8")

    assert 'const userId = String(userClaims.id ?? "").trim()' in source
    assert 'const jwtSubject = String(jwtClaims.sub ?? "").trim()' in source
    assert 'user.id !== userId' in source
    assert 'auth_user_id: userId' in source
    assert 'email: email.slice(0, 320)' in source
    assert 'status: "ACTIVE"' in source
    assert "display_name: displayName" in source
    assert "legacy customer record" in source
    assert "ctx.supabaseAdmin" in source


def test_paper_staging_customer_profile_provision_requires_aal2_and_confirms_email():
    source = (ROOT / "app/paper_staging.py").read_text(encoding="utf-8")

    provision = source[
        source.index('async def paper_customer_profile_provision'):
        source.index('@app.get("/api/customer/me")')
    ]
    assert "customer_claims(authorization, require_aal2=True)" in provision
    assert "_supabase_user(token)" in provision
    assert 'user.get("email_confirmed_at")' in provision
    assert '"/functions/v1/customer-profile-provision"' in provision


def test_paper_dashboard_does_not_claim_market_equity_without_verified_position_marks():
    source = (ROOT / "app/paper_staging.py").read_text(encoding="utf-8")

    assert 'if cash is not None and not positions' in source
    assert '"CURRENT_MARK_REQUIRED"' in source
    assert '"virtual_only": True' in source


def test_customer_profile_provisioning_never_writes_plaintext_into_encrypted_profile_columns():
    source = (
        ROOT / "supabase/functions/customer-profile-provision/index.ts"
    ).read_text(encoding="utf-8")
    insert = source[
        source.index(".insert({"):
        source.index("})", source.index(".insert({")) + 2
    ]

    assert "auth_user_id: userId" in insert
    assert 'status: "ACTIVE"' in insert
    assert "created_at: now" in insert
    assert "updated_at: now" in insert
    assert "email:" not in insert
    assert "display_name:" not in insert
    assert ".ilike(" not in source


def test_customer_me_reads_confirmed_pii_from_supabase_auth_not_encrypted_db_columns():
    source = (ROOT / "app/paper_staging.py").read_text(encoding="utf-8")
    me = source[
        source.index('async def paper_customer_me'):
        source.index('@app.get("/api/customer/portfolio")')
    ]

    assert "_supabase_user(_bearer_token(authorization))" in me
    assert 'user.get("email_confirmed_at")' in me
    assert 'user.get("email")' in me
    assert "CustomerProfile.email" not in me
    assert "CustomerProfile.display_name" not in me
    assert "select(" not in me
