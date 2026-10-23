import java.net.URI

plugins {
    id("com.android.application")
    id("org.jetbrains.kotlin.android")
    id("org.jetbrains.kotlin.plugin.compose")
}

val configuredBackendUrlRaw = providers.environmentVariable("ATLAS_PROD_BACKEND_URL").orNull?.trim()
val configuredBackendUrl = configuredBackendUrlRaw
    ?.takeIf { value -> value.none { char -> char == '"' || char == '\\' || char == '\n' || char == '\r' } }
    ?.trimEnd('/')
val configuredDevBackendUrlRaw = providers.environmentVariable("ATLAS_DEV_BACKEND_URL").orNull
val configuredDevBackendUrl = configuredDevBackendUrlRaw?.trim()
val normalizedDevBackendUrl = configuredDevBackendUrl?.trimEnd('/')
// Debug APKs must never receive a blank backend URL or silently inherit production.
// Developers can provide an explicit development/staging origin; otherwise this reserved
// non-routable origin fails visibly instead of sending traffic to an unintended service.
val debugBackendUrl = if (configuredDevBackendUrl.isNullOrEmpty()) {
    "https://atlas-debug-backend.invalid"
} else {
    check(!normalizedDevBackendUrl.isNullOrEmpty()) {
        "ATLAS_DEV_BACKEND_URL normalizes to an empty value; provide an HTTP(S) origin with a host."
    }
    check(configuredDevBackendUrl.none { char ->
        char == '"' || char == '\\' || char == '\n' || char == '\r'
    }) {
        "ATLAS_DEV_BACKEND_URL contains invalid characters."
    }
    val explicitDevUri = runCatching { URI(configuredDevBackendUrl) }.getOrNull()
    check(
        explicitDevUri != null &&
            explicitDevUri.scheme?.lowercase() in setOf("http", "https") &&
            !explicitDevUri.host.isNullOrBlank() &&
            explicitDevUri.rawUserInfo == null &&
            explicitDevUri.rawQuery == null &&
            explicitDevUri.rawFragment == null &&
            (explicitDevUri.rawPath.isNullOrEmpty() || explicitDevUri.rawPath == "/") &&
            explicitDevUri.port in -1..65535
    ) {
        "ATLAS_DEV_BACKEND_URL must be a valid HTTP(S) origin with a host, and no credentials, path, query, or fragment."
    }
    normalizedDevBackendUrl
}
val parsedBackendUri = configuredBackendUrl?.let { runCatching { URI(it) }.getOrNull() }
val rawBackendHost = parsedBackendUri?.host?.lowercase().orEmpty()
val backendHost = rawBackendHost.removeSuffix(".")
val hostHasMultipleTerminalDots = rawBackendHost.endsWith("..")
val nonProductionHostPattern = Regex("(^|[.-])(dev|development|staging|test|sandbox|localhost)([.-]|$)", RegexOption.IGNORE_CASE)
val privateIpv4Pattern = Regex("^(10\\.|127\\.|169\\.254\\.|192\\.168\\.|172\\.(1[6-9]|2[0-9]|3[01])\\.)")
val approvedProductionBackend = parsedBackendUri != null &&
    parsedBackendUri.scheme == "https" &&
    backendHost.contains(".") &&
    !hostHasMultipleTerminalDots &&
    parsedBackendUri.rawUserInfo == null &&
    parsedBackendUri.rawQuery == null &&
    parsedBackendUri.rawFragment == null &&
    parsedBackendUri.port in listOf(-1, 443) &&
    (parsedBackendUri.rawPath.isNullOrEmpty() || parsedBackendUri.rawPath == "/") &&
    !nonProductionHostPattern.containsMatchIn(backendHost) &&
    !privateIpv4Pattern.containsMatchIn(backendHost) &&
    !backendHost.startsWith("[") &&
    backendHost !in setOf("example.com", "example.org", "example.net") &&
    !backendHost.endsWith(".invalid") &&
    !backendHost.endsWith(".local") &&
    !backendHost.endsWith(".internal")

// The release check is attached to AGP's release pre-build task, not to the requested
// task-name strings. It therefore runs for assembleRelease, bundleRelease, build, and
// any aggregate task whose dependency graph includes the release variant.
val verifyProductionReleaseConfiguration = tasks.register("verifyProductionReleaseConfiguration") {
    group = "verification"
    doLast {
        check(approvedProductionBackend) {
            "BLOCKED: ATLAS_PROD_BACKEND_URL must be configured as an approved HTTPS production origin; development, staging, test, link-local/private, example, and non-root URLs are rejected."
        }
        check(
            !providers.environmentVariable("ATLAS_RELEASE_STORE_FILE").orNull.isNullOrBlank() &&
                !providers.environmentVariable("ATLAS_RELEASE_STORE_PASSWORD").orNull.isNullOrBlank() &&
                !providers.environmentVariable("ATLAS_RELEASE_KEY_ALIAS").orNull.isNullOrBlank() &&
                !providers.environmentVariable("ATLAS_RELEASE_KEY_PASSWORD").orNull.isNullOrBlank()
        ) {
            "Production release signing is not configured. Provide the ATLAS_RELEASE_* signing environment variables."
        }
    }
}
tasks.configureEach {
    if (name == "preReleaseBuild") dependsOn(verifyProductionReleaseConfiguration)
}

android {
    namespace = "com.atlas.trading"
    compileSdk = 35

    defaultConfig {
        applicationId = "com.atlas.trading"
        minSdk = 26
        targetSdk = 35
        versionCode = 1047
        versionName = "3.10.47"
        buildConfigField("String", "BACKEND_URL", "\"" + debugBackendUrl + "\"")
    }

    buildFeatures {
        compose = true
        buildConfig = true
    }

    compileOptions {
        sourceCompatibility = JavaVersion.VERSION_21
        targetCompatibility = JavaVersion.VERSION_21
    }

    kotlinOptions {
        jvmTarget = "21"
    }

    // Never sign a production artifact with the Android debug keystore.
    val releaseStoreFile = providers.environmentVariable("ATLAS_RELEASE_STORE_FILE").orNull
    val releaseStorePassword = providers.environmentVariable("ATLAS_RELEASE_STORE_PASSWORD").orNull
    val releaseKeyAlias = providers.environmentVariable("ATLAS_RELEASE_KEY_ALIAS").orNull
    val releaseKeyPassword = providers.environmentVariable("ATLAS_RELEASE_KEY_PASSWORD").orNull

    signingConfigs {
        create("releaseConfig") {
            if (!releaseStoreFile.isNullOrBlank() && !releaseStorePassword.isNullOrBlank() &&
                !releaseKeyAlias.isNullOrBlank() && !releaseKeyPassword.isNullOrBlank()) {
                storeFile = file(releaseStoreFile)
                storePassword = releaseStorePassword
                keyAlias = releaseKeyAlias
                keyPassword = releaseKeyPassword
            }
        }
        val localKeystore = file("${rootDir}/debug.keystore")
        if (localKeystore.exists()) {
            create("debugConfig") {
                storeFile = localKeystore
                storePassword = "android"
                keyAlias = "androiddebugkey"
                keyPassword = "android"
            }
        }
    }

    buildTypes {
        debug {
            buildConfigField("String", "BACKEND_URL", "\"" + debugBackendUrl + "\"")
            signingConfigs.findByName("debugConfig")?.let {
                signingConfig = it
            }
        }
        release {
            buildConfigField("String", "BACKEND_URL", "\"" + (configuredBackendUrl ?: "") + "\"")
            isMinifyEnabled = true
            isShrinkResources = true
            proguardFiles(
                getDefaultProguardFile("proguard-android-optimize.txt"),
                "proguard-rules.pro"
            )
            signingConfig = signingConfigs.getByName("releaseConfig")
        }
    }
}

dependencies {
    implementation("androidx.core:core-ktx:1.15.0")
    implementation("androidx.appcompat:appcompat:1.7.0")
    implementation("androidx.webkit:webkit:1.12.1")
    implementation("androidx.activity:activity-compose:1.9.3")
    implementation("androidx.compose.ui:ui:1.7.5")
    implementation("androidx.compose.ui:ui-graphics:1.7.5")
    implementation("androidx.compose.ui:ui-tooling-preview:1.7.5")
    implementation("androidx.compose.material3:material3:1.3.1")
    implementation("androidx.lifecycle:lifecycle-viewmodel-compose:2.8.7")
    implementation("androidx.lifecycle:lifecycle-runtime-compose:2.8.7")
    implementation("androidx.biometric:biometric:1.1.0")
    implementation("com.squareup.okhttp3:okhttp:4.12.0")
    testImplementation("junit:junit:4.13.2")
    testImplementation("org.json:json:20240303")
    testImplementation("com.squareup.okhttp3:mockwebserver:4.12.0")
}
