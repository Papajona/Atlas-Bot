from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
GRADLE = (ROOT / "app" / "build.gradle.kts").read_text(encoding="utf-8")
MAIN_ACTIVITY = (
    ROOT / "app" / "src" / "main" / "java" / "com" / "atlas" / "trading" / "MainActivity.kt"
).read_text(encoding="utf-8")
AUTH_CLIENT = (
    ROOT / "app" / "src" / "main" / "java" / "com" / "atlas" / "trading" / "auth" / "AdminAuthClient.kt"
).read_text(encoding="utf-8")
STRINGS = (ROOT / "app" / "src" / "main" / "res" / "values" / "strings.xml").read_text(encoding="utf-8")
RELEASE_WORKFLOW = (ROOT / ".github" / "workflows" / "android-release.yml").read_text(encoding="utf-8")


def test_release_build_requires_an_explicit_approved_https_backend_origin():
    assert GRADLE.startswith("import java.net.URI\\n\\nplugins {")
    assert 'providers.environmentVariable("ATLAS_PROD_BACKEND_URL")' in GRADLE
    assert "releaseTaskRequested" in GRADLE
    assert GRADLE.startswith("import java.net.URI\\n\\nplugins {")
    assert "runCatching { URI(it) }" in GRADLE
    assert 'check(approvedProductionBackend)' in GRADLE
    assert 'parsedBackendUri.scheme == "https"' in GRADLE
    assert "nonProductionHostPattern.containsMatchIn(backendHost)" in GRADLE
    assert "privateIpv4Pattern.containsMatchIn(backendHost)" in GRADLE
    assert 'buildConfigField("String", "BACKEND_URL"' in GRADLE


def test_signed_release_workflow_passes_the_backend_and_fails_closed_if_missing():
    assert "ATLAS_PROD_BACKEND_URL: ${{ vars.ATLAS_PROD_BACKEND_URL }}" in RELEASE_WORKFLOW
    assert 'test -n "$ATLAS_PROD_BACKEND_URL"' in RELEASE_WORKFLOW
    assert "gradle --no-daemon :app:assembleRelease" in RELEASE_WORKFLOW


def test_android_does_not_ship_or_reference_the_checked_in_development_service_url():
    assert "ais-dev-fizvnhkrtyabao5zevmtci" not in STRINGS
    assert 'name="backend_url"' not in STRINGS
    assert 'name="trusted_web_host"' not in STRINGS
    assert "R.string.backend_url" not in MAIN_ACTIVITY
    assert "R.string.trusted_web_host" not in MAIN_ACTIVITY
    assert "BuildConfig.BACKEND_URL.trimEnd('/')" in MAIN_ACTIVITY
    assert "Uri.parse(BuildConfig.BACKEND_URL).host.orEmpty().lowercase()" in MAIN_ACTIVITY


def test_admin_authentication_has_bounded_network_timeouts_and_screen_is_secure():
    assert ".connectTimeout(10, TimeUnit.SECONDS)" in AUTH_CLIENT
    assert ".callTimeout(15, TimeUnit.SECONDS)" in AUTH_CLIENT
    assert "WindowManager.LayoutParams.FLAG_SECURE" in MAIN_ACTIVITY


def test_android_biometric_dependency_is_stable_not_alpha():
    assert 'implementation("androidx.biometric:biometric:1.1.0")' in GRADLE
    assert "biometric:1.2.0-alpha05" not in GRADLE
