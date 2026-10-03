package com.atlas.trading

import android.annotation.SuppressLint
import android.content.Intent
import android.graphics.Color
import android.net.Uri
import android.os.Bundle
import android.view.View
import android.webkit.WebResourceError
import android.webkit.WebResourceRequest
import android.webkit.WebView
import android.webkit.WebViewClient
import androidx.appcompat.app.AppCompatActivity
import androidx.core.view.WindowCompat
import androidx.webkit.WebSettingsCompat
import androidx.webkit.WebViewFeature

class MainActivity : AppCompatActivity() {
    private lateinit var web: WebView

    @SuppressLint("SetJavaScriptEnabled")
    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        WindowCompat.setDecorFitsSystemWindows(window, true)
        window.statusBarColor = Color.rgb(7, 11, 24)
        window.navigationBarColor = Color.rgb(7, 11, 24)
        WindowCompat.getInsetsController(window, window.decorView).isAppearanceLightStatusBars = false
        WindowCompat.getInsetsController(window, window.decorView).isAppearanceLightNavigationBars = false

        web = WebView(this)
        web.setLayerType(View.LAYER_TYPE_SOFTWARE, null)
        setContentView(web)
        web.settings.javaScriptEnabled = true
        web.settings.domStorageEnabled = true
        web.settings.databaseEnabled = false
        web.settings.allowFileAccess = false
        web.settings.allowContentAccess = false
        web.settings.setSupportZoom(false)
        if (WebViewFeature.isFeatureSupported(WebViewFeature.FORCE_DARK)) {
            WebSettingsCompat.setForceDark(web.settings, WebSettingsCompat.FORCE_DARK_OFF)
        }
        web.webViewClient = object : WebViewClient() {
            override fun shouldOverrideUrlLoading(view: WebView, request: WebResourceRequest): Boolean {
                val uri = request.url
                if (uri.scheme != "https") return true
                val backendHost = Uri.parse(getString(R.string.backend_url)).host.orEmpty()
                val trustedHost = getString(R.string.trusted_web_host).trim().lowercase()
                val checkoutHost = getString(R.string.trusted_checkout_host).trim().lowercase()
                val host = uri.host.orEmpty().lowercase()
                val allowed = host == backendHost || (trustedHost.isNotBlank() && trustedHost != "YOUR-CUSTOMER-DOMAIN" && host == trustedHost) || host == checkoutHost
                if (!allowed) {
                    startActivity(Intent(Intent.ACTION_VIEW, uri))
                    return true
                }
                return false
            }

            override fun onReceivedError(view: WebView, request: WebResourceRequest, error: WebResourceError) {
                if (request.isForMainFrame) {
                    val desc = error.description?.toString() ?: "Network error"
                    showFallback(desc)
                }
            }
        }
        loadBackend()
    }

    private fun loadBackend() {
        val base = getString(R.string.backend_url).trimEnd('/')
        if (base.startsWith("https://") && !base.contains("YOUR-CLOUD-RUN-URL")) {
            web.loadUrl("$base/")
        } else {
            showFallback(null)
        }
    }

    private fun showFallback(errorMsg: String?) {
        val extra = if (errorMsg != null) {
            "<p style='color:#f87171;font-size:13px;margin-top:12px'>Connection notice: $errorMsg</p><p style='margin-top:16px'><button onclick='location.reload()' style='background:#6ee7f7;color:#070b18;border:none;border-radius:8px;padding:10px 18px;font-weight:bold;cursor:pointer'>Retry Connection</button></p>"
        } else {
            "<p style='color:#93a1b8;line-height:1.6'>Set <b>backend_url</b> in <code>app/src/main/res/values/strings.xml</code> to your Cloud Run HTTPS URL, then rebuild.</p>"
        }
        val html = "<html><body style='background:#070b18;color:white;font-family:sans-serif;padding:32px'><div style='max-width:520px;margin:12vh auto;padding:28px;border:1px solid #22304b;border-radius:22px;background:#0e1729'><div style='font-size:12px;letter-spacing:.14em;color:#6ee7f7'>ATLAS TRADING</div><h2 style='font-size:28px'>Cloud Run Control Center</h2><p style='color:#93a1b8;line-height:1.6'>Automated trading & portfolio management client.</p>$extra</div></body></html>"
        web.loadDataWithBaseURL(null, html, "text/html", "UTF-8", null)
    }

    @Suppress("DEPRECATION")
    override fun onBackPressed() {
        if (web.canGoBack()) web.goBack() else super.onBackPressed()
    }

    override fun onDestroy() {
        web.destroy()
        super.onDestroy()
    }
}
