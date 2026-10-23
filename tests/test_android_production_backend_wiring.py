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
    assert GRADLE.startswith("import java.net.URI\n\nplugins {")
    assert 'providers.environmentVariable("ATLAS_PROD_BACKEND_URL")' in GRADLE
    assert "runCatching { URI(it) }" in GRADLE
    assert "tasks.configureEach" in GRADLE
    assert 'if (name == "preReleaseBuild") dependsOn(verifyProductionReleaseConfiguration)' in GRADLE
    assert 'group = "verification"' in GRADLE
    assert 'check(approvedProductionBackend)' in GRADLE
    assert 'parsedBackendUri.scheme == "https"' in GRADLE
    assert "nonProductionHostPattern.containsMatchIn(backendHost)" in GRADLE
    assert "privateIpv4Pattern.containsMatchIn(backendHost)" in GRADLE
    assert r'169\\.254\\.' in GRADLE
    assert "rawBackendHost.removeSuffix(\".\")" in GRADLE
    assert "hostHasMultipleTerminalDots" in GRADLE
    assert 'ATLAS_DEV_BACKEND_URL' in GRADLE
    assert 'https://atlas-debug-backend.invalid' in GRADLE
    assert 'buildConfigField("String", "BACKEND_URL"' in GRADLE


def test_signed_release_workflow_accepts_verified_dispatch_origin_and_fails_closed_if_missing():
    assert "production_backend_url:" in RELEASE_WORKFLOW
    assert 'description: "Verified public HTTPS Atlas origin behind the configured Cloudflare/Google HTTPS load balancer; do not use a staging URL."' in RELEASE_WORKFLOW
    assert "required: true" in RELEASE_WORKFLOW
    assert "ATLAS_PROD_BACKEND_URL: ${{ inputs.production_backend_url || vars.ATLAS_PROD_BACKEND_URL }}" in RELEASE_WORKFLOW
    assert 'test -n "$ATLAS_PROD_BACKEND_URL"' in RELEASE_WORKFLOW
    assert "configure ATLAS_PROD_BACKEND_URL for a signed release tag" in RELEASE_WORKFLOW
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


def test_explicit_debug_backend_url_is_normalized_then_validated_as_an_origin():
    assert 'val normalizedDevBackendUrl = configuredDevBackendUrl?.trimEnd(\'/\')' in GRADLE
    assert 'check(!normalizedDevBackendUrl.isNullOrEmpty())' in GRADLE
    assert 'explicitDevUri.scheme?.lowercase() in setOf("http", "https")' in GRADLE
    assert '!explicitDevUri.host.isNullOrBlank()' in GRADLE
    assert 'explicitDevUri.rawUserInfo == null' in GRADLE
    assert 'explicitDevUri.rawQuery == null' in GRADLE
    assert 'explicitDevUri.rawFragment == null' in GRADLE
    assert '(explicitDevUri.rawPath.isNullOrEmpty() || explicitDevUri.rawPath == "/")' in GRADLE


def test_signed_release_is_only_triggered_by_an_explicit_release_tag_or_manual_main_dispatch():
    push_trigger = RELEASE_WORKFLOW.split("  push:\n", 1)[1].split("\npermissions:\n", 1)[0]
    release_job = RELEASE_WORKFLOW.split("  android-release:\n", 1)[1].split("    runs-on:", 1)[0]

    assert 'tags:\n      - "android-v*"' in push_trigger
    assert "branches:" not in push_trigger
    assert "paths:" not in push_trigger
    assert "github.event_name == 'workflow_dispatch' && github.ref == 'refs/heads/main'" in release_job
    assert "github.event_name == 'push' && startsWith(github.ref, 'refs/tags/android-v')" in release_job
    assert "github.event_name == 'push' && github.ref == 'refs/heads/main'" not in release_job
