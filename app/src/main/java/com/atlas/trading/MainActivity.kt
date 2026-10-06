package com.atlas.trading

import android.content.Intent
import android.net.Uri
import android.os.Bundle
import android.widget.Toast
import androidx.compose.foundation.layout.*
import androidx.compose.material3.*
import androidx.compose.runtime.*
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.text.input.PasswordVisualTransformation
import androidx.compose.ui.unit.dp
import kotlinx.coroutines.launch
import com.atlas.trading.auth.AdminAuthClient
import com.atlas.trading.security.SecurityAuditLog
import androidx.activity.compose.BackHandler
import androidx.activity.compose.setContent
import androidx.activity.enableEdgeToEdge
import androidx.appcompat.app.AppCompatActivity
import androidx.biometric.BiometricManager
import androidx.biometric.BiometricPrompt
import androidx.core.content.ContextCompat
import com.atlas.trading.ui.PortfolioRiskMetrics
import com.atlas.trading.ui.RiskDashboardScreen
import com.atlas.trading.ui.UserDashboardScreen
import com.atlas.trading.ui.theme.AppThemeMode
import com.atlas.trading.ui.theme.AtlasTheme
import com.atlas.trading.ui.theme.ThemePreferences
import androidx.compose.foundation.isSystemInDarkTheme
import androidx.core.view.WindowCompat
import java.io.File

class MainActivity : AppCompatActivity() {

    private val securityAuditLog by lazy { SecurityAuditLog(applicationContext) }

    override fun onCreate(savedInstanceState: Bundle?) {
        enableEdgeToEdge()
        super.onCreate(savedInstanceState)

        cleanLegacyCache()

        val backendUrl = getString(R.string.backend_url).trimEnd('/')

        setContent {
            var themeMode by remember { mutableStateOf(ThemePreferences.getThemeMode(this@MainActivity)) }
            val isSystemDark = isSystemInDarkTheme()
            val isDark = when (themeMode) {
                AppThemeMode.LIGHT -> false
                AppThemeMode.DARK -> true
                AppThemeMode.SYSTEM -> isSystemDark
            }

            LaunchedEffect(isDark) {
                WindowCompat.getInsetsController(window, window.decorView).apply {
                    isAppearanceLightStatusBars = !isDark
                    isAppearanceLightNavigationBars = !isDark
                }
            }

            AtlasTheme(
                themeMode = themeMode,
                onThemeChange = { newMode ->
                    themeMode = newMode
                    ThemePreferences.setThemeMode(this@MainActivity, newMode)
                }
            ) {
                var showOperatorConsole by remember { mutableStateOf(false) }
                var accessToken by remember { mutableStateOf<String?>(null) }
                var loginError by remember { mutableStateOf<String?>(null) }
                var mfaChallenge by remember { mutableStateOf<AdminAuthClient.MfaChallenge?>(null) }
                val scope = rememberCoroutineScope()
                val authClient = remember { AdminAuthClient(backendUrl) }

                BackHandler(enabled = showOperatorConsole) {
                    showOperatorConsole = false
                }

                if (!showOperatorConsole) {
                    UserDashboardScreen(
                        userName = "Alice",
                        backendUrl = backendUrl,
                        onOpenOperatorConsole = { showOperatorConsole = true },
                        onOpenPortal = { openPortal() }
                    )
                } else if (accessToken == null) {
                    AdminLoginScreen(
                        error = loginError,
                        mfaChallenge = mfaChallenge,
                        onLogin = { email, password ->
                            scope.launch {
                                loginError = null
                                try {
                                    val result = authClient.login(email, password)
                                    if (result.accessToken != null) {
                                        accessToken = result.accessToken
                                        mfaChallenge = null
                                    } else if (result.challenge != null) {
                                        mfaChallenge = result.challenge
                                    } else {
                                        loginError = result.message
                                    }
                                } catch (e: Exception) {
                                    loginError = e.message ?: "Administrator authentication failed"
                                }
                            }
                        },
                        onVerifyMfa = { code ->
                            scope.launch {
                                loginError = null
                                try {
                                    accessToken = authClient.verifyMfa(code)
                                    mfaChallenge = null
                                } catch (e: Exception) {
                                    loginError = e.message ?: "Authenticator verification failed"
                                }
                            }
                        },
                        onBack = { showOperatorConsole = false }
                    )
                } else {
                    RiskDashboardScreen(
                        backendUrl = backendUrl,
                        accessToken = accessToken,
                        initialMetrics = PortfolioRiskMetrics(),
                        onOpenPortal = { openPortal() },
                        onRequestBiometricResume = { onSuccess ->
                            authenticateBiometricForResume(onSuccess)
                        },
                        onEmergencyHaltToggle = { isHalted ->
                            val token = accessToken
                            scope.launch {
                                try {
                                    runCatching { securityAuditLog.append("RISK_KILL_SWITCH_REQUESTED", mapOf("halted" to isHalted)) }
                                    val state = authClient.setKillSwitch(isHalted, token)
                                    if (!state.confirmed) throw IllegalStateException("Server did not confirm requested risk state")
                                    runCatching { securityAuditLog.append("RISK_KILL_SWITCH_CONFIRMED", mapOf("halted" to isHalted)) }
                                    Toast.makeText(
                                        this@MainActivity,
                                        if (isHalted) "SERVER CONFIRMED: ENGINE HALTED" else "SERVER CONFIRMED: HALT RELEASED; LIVE TRADING REMAINS DISABLED",
                                        Toast.LENGTH_LONG
                                    ).show()
                                } catch (e: Exception) {
                                    Toast.makeText(this@MainActivity, "Risk state change failed: ${e.message}", Toast.LENGTH_LONG).show()
                                }
                            }
                        },
                        onBack = { showOperatorConsole = false }
                    )
                }
            }
        }
    }

    private fun authenticateBiometricForResume(onSuccess: () -> Unit) {
        val executor = ContextCompat.getMainExecutor(this)
        val biometricManager = BiometricManager.from(this)
        val canAuthenticate = biometricManager.canAuthenticate(
            BiometricManager.Authenticators.BIOMETRIC_STRONG or BiometricManager.Authenticators.DEVICE_CREDENTIAL
        )

        if (canAuthenticate == BiometricManager.BIOMETRIC_SUCCESS) {
            val promptInfo = BiometricPrompt.PromptInfo.Builder()
                .setTitle("Atlas Security: Disarm Kill Switch")
                .setSubtitle("Biometric authentication required to authorize risk reset")
                .setDescription("Scan fingerprint or use device credentials to authorize release of the application halt. Live trading remains disabled until separately enabled.")
                .setAllowedAuthenticators(
                    BiometricManager.Authenticators.BIOMETRIC_STRONG or BiometricManager.Authenticators.DEVICE_CREDENTIAL
                )
                .build()

            val prompt = BiometricPrompt(this, executor, object : BiometricPrompt.AuthenticationCallback() {
                override fun onAuthenticationSucceeded(result: BiometricPrompt.AuthenticationResult) {
                    super.onAuthenticationSucceeded(result)
                    runCatching { securityAuditLog.append("BIOMETRIC_AUTH_SUCCEEDED", mapOf("purpose" to "RISK_RESET")) }
                    Toast.makeText(this@MainActivity, "Biometrics verified. Requesting server risk reset…", Toast.LENGTH_SHORT).show()
                    onSuccess()
                }

                override fun onAuthenticationError(errorCode: Int, errString: CharSequence) {
                    super.onAuthenticationError(errorCode, errString)
                    runCatching { securityAuditLog.append("BIOMETRIC_AUTH_ERROR", mapOf("purpose" to "RISK_RESET", "error_code" to errorCode)) }
                    Toast.makeText(this@MainActivity, "Auth canceled: $errString", Toast.LENGTH_SHORT).show()
                }

                override fun onAuthenticationFailed() {
                    super.onAuthenticationFailed()
                    runCatching { securityAuditLog.append("BIOMETRIC_AUTH_FAILED", mapOf("purpose" to "RISK_RESET")) }
                    Toast.makeText(this@MainActivity, "Biometric unrecognized", Toast.LENGTH_SHORT).show()
                }
            })
            prompt.authenticate(promptInfo)
        } else {
            // Never bypass the operator gate. A device without a supported/enrolled
            // authenticator cannot authorize resuming live trading.
            Toast.makeText(
                this,
                "Resume blocked: strong biometric/device credential is required.",
                Toast.LENGTH_LONG
            ).show()
        }
    }

    private fun openPortal() {
        val backendUrl = getString(R.string.backend_url).trimEnd('/')
        val trustedHost = getString(R.string.trusted_web_host).trim().lowercase()
        val checkoutHost = getString(R.string.trusted_checkout_host).trim().lowercase()

        val targetUri = Uri.parse(backendUrl)
        val host = targetUri.host.orEmpty().lowercase()
        val isAllowed = host == trustedHost || host == checkoutHost || host.endsWith(".run.app")

        if (isAllowed && backendUrl.startsWith("https://")) {
            val browserIntent = Intent(Intent.ACTION_VIEW, targetUri)
            try {
                startActivity(browserIntent)
            } catch (e: Exception) {
                Toast.makeText(this, "Opening: $backendUrl", Toast.LENGTH_SHORT).show()
            }
        } else {
            Toast.makeText(this, "Untrusted destination host: $host", Toast.LENGTH_LONG).show()
        }
    }

    private fun cleanLegacyCache() {
        try {
            val webViewDir = File(cacheDir, "WebView")
            if (webViewDir.exists()) {
                webViewDir.deleteRecursively()
            }
        } catch (_: Throwable) {}
    }
}

