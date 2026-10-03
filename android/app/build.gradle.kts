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

    // Release signing is deliberately CI/secret-manager driven. A debug keystore must
    // never be used to sign a production artifact.
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
    }

    buildTypes {
        debug {
            // Debug-only signing is permitted for local development.
        }
        release {
            isMinifyEnabled = true
            isShrinkResources = true
            proguardFiles(
                getDefaultProguardFile("proguard-android-optimize.txt"),
                "proguard-rules.pro"
            )
            check(
                !releaseStoreFile.isNullOrBlank() &&
                !releaseStorePassword.isNullOrBlank() &&
                !releaseKeyAlias.isNullOrBlank() &&
                !releaseKeyPassword.isNullOrBlank()
            ) {
                "Production release signing is not configured. Supply ATLAS_RELEASE_STORE_FILE, " +
                "ATLAS_RELEASE_STORE_PASSWORD, ATLAS_RELEASE_KEY_ALIAS and ATLAS_RELEASE_KEY_PASSWORD."
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
    implementation("androidx.biometric:biometric:1.2.0-alpha05")
    implementation("com.squareup.okhttp3:okhttp:4.12.0")
    testImplementation("junit:junit:4.13.2")
}
