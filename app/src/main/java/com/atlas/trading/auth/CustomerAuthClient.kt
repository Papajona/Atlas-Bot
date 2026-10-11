package com.atlas.trading.auth

import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.withContext
import okhttp3.MediaType.Companion.toMediaType
import okhttp3.OkHttpClient
import okhttp3.Request
import okhttp3.RequestBody.Companion.toRequestBody
import org.json.JSONObject

/** Customer login uses the existing backend proxy; Supabase credentials never go to a third party. */
class CustomerAuthClient(
    private val baseUrl: String,
    private val http: OkHttpClient = OkHttpClient()
) {
    data class Session(val accessToken: String, val refreshToken: String?)

    suspend fun login(email: String, password: String): Session = withContext(Dispatchers.IO) {
        require(email.isNotBlank() && email.contains("@")) { "Enter a valid email address" }
        require(password.isNotBlank()) { "Password is required" }
        require(baseUrl.isNotBlank()) { "Customer authentication service is unavailable" }

        val body = JSONObject().put("email", email.trim()).put("password", password)
        val request = Request.Builder()
            .url(baseUrl.trimEnd('/') + "/api/auth/login")
            .post(body.toString().toRequestBody("application/json".toMediaType()))
            .header("Accept", "application/json")
            .header("Content-Type", "application/json")
            .build()
        http.newCall(request).execute().use { response ->
            val raw = response.body?.string().orEmpty()
            val json = runCatching { JSONObject(raw) }.getOrElse { JSONObject() }
            if (!response.isSuccessful) {
                throw IllegalStateException(
                    json.optString("detail").takeIf { it.isNotBlank() }
                        ?: "Customer sign-in failed (HTTP ${response.code})"
                )
            }
            val token = json.optString("access_token").takeIf { it.isNotBlank() && it != "null" }
                ?: throw IllegalStateException("Customer sign-in did not return an access token")
            Session(
                accessToken = token,
                refreshToken = json.optString("refresh_token").takeIf { it.isNotBlank() && it != "null" }
            )
        }
    }
}
