from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from scripts import verify_android_apk_backend_origin as verifier


def _resource_dump(entries: list[tuple[str, str]]) -> str:
    lines: list[str] = []
    for index, (name, value) in enumerate(entries, start=1):
        lines.extend(
            [
                f"resource 0x7f1200{index:02x} com.atlas.trading:string/{name}",
                f'  () (string8) "{value}"',
            ]
        )
    return "\n".join(lines) + "\n"


def test_normalizes_https_origin_and_runtime_url():
    literal, origin = verifier.normalize_backend_url("  https://ATLAS.EXAMPLE.COM:443/  ")
    assert literal == "https://ATLAS.EXAMPLE.COM:443"
    assert origin == "https://atlas.example.com"


@pytest.mark.parametrize(
    "value",
    [
        "http://atlas.example.com",
        "https://user:pass@atlas.example.com",
        "https://atlas.example.com/api",
        "https://atlas.example.com?other=1",
        "https://atlas.example.com?",
        "https://atlas.example.com#",
        "https://atlas.example.com:8443",
        "https://localhost",
        "https://staging.atlas.example.com",
        "https://192.0.2.10",
    ],
)
def test_rejects_urls_that_are_not_approved_https_production_origins(value: str):
    with pytest.raises(ValueError):
        verifier.normalize_backend_url(value)


def test_extracts_only_the_named_runtime_backend_resource():
    dump = _resource_dump(
        [
            ("telemetry_endpoint", "https://unrelated.example.com"),
            ("atlas_backend_url", "https://atlas.example.com"),
        ]
    )
    assert verifier.extract_runtime_backend_url_from_resource_dump(dump) == "https://atlas.example.com"
    assert verifier.verify_runtime_backend_origin(
        dump,
        "https://atlas.example.com",
        "https://atlas.example.com/",
    ) == {
        "backend_url": "https://atlas.example.com",
        "backend_origin": "https://atlas.example.com",
    }


def test_rejects_approved_url_elsewhere_when_active_runtime_backend_differs():
    approved_url = "https://atlas.example.com"
    active_url = "https://unrelated.example.com"
    dump = _resource_dump(
        [
            ("diagnostic_example", approved_url),
            ("atlas_backend_url", active_url),
        ]
    )

    # The approved string exists in the APK resource dump, but not in the resource
    # MainActivity actually reads. The verifier must use the named runtime resource.
    assert approved_url in dump
    assert active_url in dump
    assert verifier.extract_runtime_backend_url_from_resource_dump(dump) == active_url
    with pytest.raises(ValueError, match="effective runtime backend resource does not match"):
        verifier.verify_runtime_backend_origin(dump, approved_url, approved_url)


def test_fails_closed_when_runtime_resource_is_missing_or_ambiguous():
    with pytest.raises(ValueError, match="exactly one"):
        verifier.extract_runtime_backend_url_from_resource_dump(
            _resource_dump([("app_name", "Atlas Trading")])
        )

    ambiguous = _resource_dump(
        [
            ("atlas_backend_url", "https://atlas.example.com"),
            ("atlas_backend_url", "https://unrelated.example.com"),
        ]
    )
    with pytest.raises(ValueError, match="exactly one"):
        verifier.extract_runtime_backend_url_from_resource_dump(ambiguous)


def test_apk_verifier_decodes_resource_table_not_dex_string_presence(monkeypatch, tmp_path: Path):
    apk = tmp_path / "atlas-release.apk"
    apk.write_bytes(b"test placeholder; aapt is mocked")
    dump = _resource_dump(
        [
            ("diagnostic_example", "https://atlas.example.com"),
            ("atlas_backend_url", "https://unrelated.example.com"),
        ]
    )
    calls: list[list[str]] = []

    def fake_run(command, **kwargs):
        calls.append(command)
        return SimpleNamespace(returncode=0, stdout=dump, stderr="")

    monkeypatch.setattr(verifier.subprocess, "run", fake_run)
    with pytest.raises(ValueError, match="effective runtime backend resource does not match"):
        verifier.verify_apk_backend_origin(
            apk,
            "https://atlas.example.com",
            "https://atlas.example.com",
            "android-sdk/build-tools/35.0.0/aapt",
        )
    assert calls == [
        [
            "android-sdk/build-tools/35.0.0/aapt",
            "dump",
            "resources",
            str(apk),
        ]
    ]


def test_fails_closed_if_aapt_cannot_decode_the_apk(monkeypatch, tmp_path: Path):
    apk = tmp_path / "broken.apk"
    apk.write_bytes(b"not an apk")
    monkeypatch.setattr(
        verifier.subprocess,
        "run",
        lambda *args, **kwargs: SimpleNamespace(returncode=1, stdout="", stderr="bad resource table"),
    )
    with pytest.raises(ValueError, match="aapt could not decode"):
        verifier.verify_apk_backend_origin(apk, "https://atlas.example.com", aapt_path="aapt")


ROOT = Path(__file__).resolve().parents[1]
GRADLE = (ROOT / "app" / "build.gradle.kts").read_text(encoding="utf-8")
MAIN_ACTIVITY = (
    ROOT / "app" / "src" / "main" / "java" / "com" / "atlas" / "trading" / "MainActivity.kt"
).read_text(encoding="utf-8")
ANDROID_RELEASE_WORKFLOW = (
    ROOT / ".github" / "workflows" / "android-release.yml"
).read_text(encoding="utf-8")
PRODUCTION_DEPLOY_WORKFLOW = (
    ROOT / ".github" / "workflows" / "production-deploy.yml"
).read_text(encoding="utf-8")


def test_android_runtime_reads_the_same_named_resource_that_release_verification_inspects():
    assert 'resValue("string", "atlas_backend_url", debugBackendUrl)' in GRADLE
    assert 'resValue("string", "atlas_backend_url", configuredBackendUrl ?: "")' in GRADLE
    assert 'getString(R.string.atlas_backend_url).trimEnd(\'/\')' in MAIN_ACTIVITY
    assert "BuildConfig.BACKEND_URL" not in MAIN_ACTIVITY
    assert 'buildConfigField("String", "BACKEND_URL"' not in GRADLE


def test_release_and_deployment_both_verify_the_effective_runtime_resource():
    assert 'python scripts/verify_android_apk_backend_origin.py --apk "$APK" --aapt "$AAPT" --expected-url "$ATLAS_PROD_BACKEND_URL"' in ANDROID_RELEASE_WORKFLOW
    assert 'backend_url=$BACKEND_URL_FOR_EVIDENCE' in ANDROID_RELEASE_WORKFLOW
    assert 'backend_origin=$BACKEND_ORIGIN' in ANDROID_RELEASE_WORKFLOW
    assert 'python scripts/verify_android_apk_backend_origin.py --apk "$apk" --aapt "$aapt" --expected-url "$metadata_backend_url" --compare-origin "$ATLAS_BASE_URL"' in PRODUCTION_DEPLOY_WORKFLOW
    assert 'test "$verified_backend_origin" = "$metadata_backend_origin"' in PRODUCTION_DEPLOY_WORKFLOW
    assert PRODUCTION_DEPLOY_WORKFLOW.index("--compare-origin") < PRODUCTION_DEPLOY_WORKFLOW.index("      - name: Authenticate to Google Cloud with GitHub OIDC")
