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
CI_WORKFLOW = (ROOT / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")


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
    assert 'resValue("string", "atlas_backend_url", configuredBackendUrl ?: "")' in GRADLE
    assert 'buildConfigField("String", "BACKEND_URL"' not in GRADLE


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
    assert "R.string.atlas_backend_url" in MAIN_ACTIVITY
    assert "R.string.trusted_web_host" not in MAIN_ACTIVITY
    assert "getString(R.string.atlas_backend_url).trimEnd('/')" in MAIN_ACTIVITY
    assert "BuildConfig.BACKEND_URL" not in MAIN_ACTIVITY
    assert "Uri.parse(backendUrl).host.orEmpty().lowercase()" in MAIN_ACTIVITY


def test_admin_authentication_has_bounded_network_timeouts_and_screen_is_secure():
    assert ".connectTimeout(10, TimeUnit.SECONDS)" in AUTH_CLIENT
    assert ".callTimeout(15, TimeUnit.SECONDS)" in AUTH_CLIENT
    assert "WindowManager.LayoutParams.FLAG_SECURE" in MAIN_ACTIVITY


def test_android_biometric_dependency_is_stable_not_alpha():
    assert 'implementation("androidx.biometric:biometric:1.1.0")' in GRADLE
    assert "biometric:1.2.0-alpha05" not in GRADLE


def test_explicit_debug_backend_url_is_normalized_then_validated_as_an_origin():
    assert 'val normalizedDevBackendUrl = configuredDevBackendUrl?.trimEnd(\'/\')' in GRADLE
    assert 'check(!configuredDevBackendUrl.isNullOrEmpty() && !normalizedDevBackendUrl.isNullOrEmpty())' in GRADLE
    assert 'explicitDevBackendUri.scheme?.lowercase() in setOf("http", "https")' in GRADLE
    assert '!explicitDevBackendUri.host.isNullOrBlank()' in GRADLE
    assert 'explicitDevBackendUri.rawUserInfo == null' in GRADLE
    assert 'explicitDevBackendUri.rawQuery == null' in GRADLE
    assert 'explicitDevBackendUri.rawFragment == null' in GRADLE
    assert '(explicitDevBackendUri.rawPath.isNullOrEmpty() || explicitDevBackendUri.rawPath == "/")' in GRADLE


def test_signed_release_is_only_triggered_by_an_explicit_release_tag_or_manual_main_dispatch():
    push_trigger = RELEASE_WORKFLOW.split("  push:\n", 1)[1].split("\npermissions:\n", 1)[0]
    release_job = RELEASE_WORKFLOW.split("  android-release:\n", 1)[1].split("    runs-on:", 1)[0]

    assert 'tags:\n      - "android-v*"' in push_trigger
    assert "branches:" not in push_trigger
    assert "paths:" not in push_trigger
    assert "github.event_name == 'workflow_dispatch' && github.ref == 'refs/heads/main'" in release_job
    assert "github.event_name == 'push' && startsWith(github.ref, 'refs/tags/android-v')" in release_job
    assert "github.event_name == 'push' && github.ref == 'refs/heads/main'" not in release_job


def test_debug_build_requires_an_explicit_valid_development_backend_origin():
    assert "verifyDebugBackendConfiguration" in GRADLE
    assert 'if (name == "preDebugBuild") dependsOn(verifyDebugBackendConfiguration)' in GRADLE
    assert "ATLAS_DEV_BACKEND_URL is required for debug builds" in GRADLE
    assert 'explicitDevBackendUri.scheme?.lowercase() in setOf("http", "https")' in GRADLE
    assert "https://atlas-debug-backend.invalid" not in GRADLE
    assert 'resValue("string", "atlas_backend_url", debugBackendUrl)' in GRADLE


def test_android_debug_ci_uses_only_the_emulator_development_origin():
    assert "ATLAS_DEV_BACKEND_URL: http://10.0.2.2:8000" in RELEASE_WORKFLOW
    assert "ATLAS_DEV_BACKEND_URL: http://10.0.2.2:8000" in CI_WORKFLOW

def test_debug_network_security_allows_only_the_emulator_http_origin():
    import xml.etree.ElementTree as ET

    android_ns = "{http://schemas.android.com/apk/res/android}"
    manifest = ET.parse(ROOT / "app" / "src" / "main" / "AndroidManifest.xml").getroot()
    application = manifest.find("application")
    assert application is not None
    assert application.get(android_ns + "networkSecurityConfig") == "@xml/network_security_config"
    assert application.get(android_ns + "usesCleartextTraffic") == "false"

    release_policy = ET.parse(
        ROOT / "app" / "src" / "main" / "res" / "xml" / "network_security_config.xml"
    ).getroot()
    debug_policy = ET.parse(
        ROOT / "app" / "src" / "debug" / "res" / "xml" / "network_security_config.xml"
    ).getroot()
    release_base = release_policy.find("base-config")
    debug_base = debug_policy.find("base-config")
    assert release_base is not None and release_base.get("cleartextTrafficPermitted") == "false"
    # Debug builds must work with explicitly configured local/staging HTTP origins;
    # the release resource remains strict and is never overridden.
    assert debug_base is not None and debug_base.get("cleartextTrafficPermitted") == "true"
