package com.atlas.trading.network

import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.withContext
import okhttp3.MediaType.Companion.toMediaType
import okhttp3.OkHttpClient
import okhttp3.Request
import okhttp3.RequestBody.Companion.toRequestBody
import org.json.JSONObject
import java.io.IOException
import java.util.concurrent.TimeUnit

data class ReleaseAdvisorResult(
    val runId: String,
    val status: String,
    val model: String,
    val diagnostics: String
)

/** Calls the authenticated server-side deterministic release diagnostics endpoint. */
class ReleaseAdvisorClient(
    private val baseUrl: String,
    private val http: OkHttpClient = OkHttpClient.Builder()
        .callTimeout(50, TimeUnit.SECONDS)
        .build()
) {
    suspend fun analyze(accessToken: String, logs: String, configuration: String): ReleaseAdvisorResult =
        withContext(Dispatchers.IO) {
            require(accessToken.isNotBlank()) { "Administrator session is required" }
            require(logs.isNotBlank() || configuration.isNotBlank()) {
                "Provide build logs or a configuration snippet"
            }
            val payload = JSONObject()
                .put("logs", logs)
                .put("configuration", configuration)
            val request = Request.Builder()
                .url(baseUrl.trimEnd('/') + "/api/admin/release-diagnostics")
                .header("Authorization", "Bearer $accessToken")
                .header("Accept", "application/json")
                .post(payload.toString().toRequestBody("application/json".toMediaType()))
                .build()
            http.newCall(request).execute().use { response ->
                val raw = response.body?.string().orEmpty()
                val json = runCatching { JSONObject(raw) }.getOrElse { JSONObject() }
                if (!response.isSuccessful) {
                    val detail = json.optString("detail").takeIf { it.isNotBlank() }
                        ?: when (response.code) {
                            402 -> "Release diagnostics are temporarily unavailable (HTTP 402)."
                            403 -> "Release advisor access is blocked for this account."
                            429 -> "Release advisor rate limit reached. Try again later."
                            else -> "Release advisor request failed (HTTP ${response.code})."
                        }
                    throw IOException(detail)
                }
                ReleaseAdvisorResult(
                    runId = json.optString("run_id"),
                    status = json.optString("status"),
                    model = json.optString("model"),
                    diagnostics = json.optString("diagnostics").ifBlank { "Server returned no diagnostic text." }
                )
            }
        }
}
