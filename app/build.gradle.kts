plugins {
    id("com.android.application")
    id("org.jetbrains.kotlin.android")
    id("org.jetbrains.kotlin.plugin.compose")
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
    }

    buildFeatures {
        compose = true
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
            signingConfigs.findByName("debugConfig")?.let {
                signingConfig = it
            }
        }
        release {
            isMinifyEnabled = true
            isShrinkResources = true
            proguardFiles(
                getDefaultProguardFile("proguard-android-optimize.txt"),
                "proguard-rules.pro"
            )
            // Evaluate the release-signing guard only when a release task is actually requested.
            // This keeps CI/debug compilation independent of production keystore secrets while
            // preserving a hard fail for every release build.
            if (gradle.startParameter.taskNames.any { it.contains("Release", ignoreCase = true) }) {
                check(
                    !releaseStoreFile.isNullOrBlank() &&
                    !releaseStorePassword.isNullOrBlank() &&
                    !releaseKeyAlias.isNullOrBlank() &&
                    !releaseKeyPassword.isNullOrBlank()
                ) {
                    "Production release signing is not configured. Provide the ATLAS_RELEASE_* signing environment variables."
                }
            }
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
}
