package com.atlas.trading

import android.content.Intent
import android.net.Uri
import android.os.Bundle
import android.view.WindowManager
import android.widget.Toast
import androidx.activity.ComponentActivity
import androidx.activity.compose.setContent
import androidx.activity.enableEdgeToEdge
import com.atlas.trading.ui.PortfolioRiskMetrics
import com.atlas.trading.ui.RiskDashboardScreen
import java.io.File

class MainActivity : ComponentActivity() {

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
