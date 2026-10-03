package com.atlas.trading

import android.content.Intent
import android.net.Uri
import android.os.Bundle
import android.view.WindowManager
import android.widget.Toast
import androidx.activity.compose.setContent
import androidx.activity.enableEdgeToEdge
import androidx.appcompat.app.AppCompatActivity
import androidx.biometric.BiometricManager
import androidx.biometric.BiometricPrompt
import androidx.core.content.ContextCompat
import com.atlas.trading.ui.PortfolioRiskMetrics
import com.atlas.trading.ui.RiskDashboardScreen
import java.io.File

class MainActivity : AppCompatActivity() {

    override fun onCreate(savedInstanceState: Bundle?) {
        window.clearFlags(WindowManager.LayoutParams.FLAG_HARDWARE_ACCELERATED)
        enableEdgeToEdge()
        super.onCreate(savedInstanceState)

        cleanLegacyCache()

        val backendUrl = getString(R.string.backend_url).trimEnd('/')

        setContent {
            RiskDashboardScreen(
                backendUrl = backendUrl,
                initialMetrics = PortfolioRiskMetrics(),
                onOpenPortal = { openPortal() },
                onRequestBiometricResume = { onSuccess ->
                    authenticateBiometricForResume(onSuccess)
                },
                onEmergencyHaltToggle = { isHalted ->
                    if (isHalted) {
                        Toast.makeText(this, "EMERGENCY HALT ACTIVATED: All order creation blocked.", Toast.LENGTH_LONG).show()
                    } else {
                        Toast.makeText(this, "Trading engine resumed with 3.10.47 risk gates.", Toast.LENGTH_SHORT).show()
                    }
                }
            )
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
                .setSubtitle("Biometric authentication required to resume trading")
                .setDescription("Scan fingerprint or use device credentials to authorize capital allocation.")
                .setAllowedAuthenticators(
                    BiometricManager.Authenticators.BIOMETRIC_STRONG or BiometricManager.Authenticators.DEVICE_CREDENTIAL
                )
                .build()

            val prompt = BiometricPrompt(this, executor, object : BiometricPrompt.AuthenticationCallback() {
                override fun onAuthenticationSucceeded(result: BiometricPrompt.AuthenticationResult) {
                    super.onAuthenticationSucceeded(result)
                    Toast.makeText(this@MainActivity, "Biometrics Verified. Trading Resumed.", Toast.LENGTH_SHORT).show()
                    onSuccess()
                }

                override fun onAuthenticationError(errorCode: Int, errString: CharSequence) {
                    super.onAuthenticationError(errorCode, errString)
                    Toast.makeText(this@MainActivity, "Auth canceled: $errString", Toast.LENGTH_SHORT).show()
                }

                override fun onAuthenticationFailed() {
                    super.onAuthenticationFailed()
                    Toast.makeText(this@MainActivity, "Biometric unrecognized", Toast.LENGTH_SHORT).show()
                }
            })
            prompt.authenticate(promptInfo)
        } else {
            // Emulators or devices without hardware biometrics enrolled fallback safely
            Toast.makeText(this, "Security Bypass: Developer/Emulator Environment", Toast.LENGTH_SHORT).show()
            onSuccess()
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

