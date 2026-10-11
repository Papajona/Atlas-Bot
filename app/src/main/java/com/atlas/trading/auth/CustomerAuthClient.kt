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
    data class MfaStatus(val enabled: Boolean, val factorIds: List<String>, val aal: String)
    data class MfaChallenge(val factorId: String, val challengeId: String)

    suspend fun mfaStatus(accessToken: String): MfaStatus = requestJson(
        "/api/auth/mfa/status", accessToken, null
    ).let { json ->
        val ids = mutableListOf<String>()
        val array = json.optJSONArray("factor_ids")
        if (array != null) for (i in 0 until array.length()) {
            array.optString(i).takeIf { it.isNotBlank() && it != "null" }?.let(ids::add)
        }
        MfaStatus(json.optBoolean("enabled"), ids, json.optString("aal", "aal1"))
    }

    suspend fun challenge(accessToken: String, factorId: String): MfaChallenge {
        require(factorId.isNotBlank()) { "Authenticator factor is unavailable" }
        val json = requestJson(
            "/api/auth/mfa/challenge", accessToken,
            JSONObject().put("factor_id", factorId)
        )
        val challengeId = json.optString("id").takeIf { it.isNotBlank() && it != "null" }
            ?: throw IllegalStateException("Authenticator challenge did not return an ID")
        return MfaChallenge(factorId, challengeId)
    }

    suspend fun verify(accessToken: String, challenge: MfaChallenge, code: String): Session {
        require(code.matches(Regex("\\d{6}"))) { "Enter the 6-digit authenticator code" }
        val json = requestJson(
            "/api/auth/mfa/verify", accessToken,
            JSONObject().put("factor_id", challenge.factorId)
                .put("challenge_id", challenge.challengeId)
                .put("code", code)
        )
        val token = json.optString("access_token").takeIf { it.isNotBlank() && it != "null" }
            ?: throw IllegalStateException("Authenticator verification did not return a refreshed session")
        return Session(token, json.optString("refresh_token").takeIf { it.isNotBlank() && it != "null" })
    }

    private suspend fun requestJson(path: String, token: String, body: JSONObject?): JSONObject =
        withContext(Dispatchers.IO) {
            val builder = Request.Builder()
                .url(baseUrl.trimEnd('/') + path)
                .header("Accept", "application/json")
                .header("Authorization", "Bearer $token")
            if (body != null) builder.post(body.toString().toRequestBody("application/json".toMediaType()))
            else builder.get()
            http.newCall(builder.build()).execute().use { response ->
                val raw = response.body?.string().orEmpty()
                val json = runCatching { JSONObject(raw) }.getOrElse { JSONObject() }
                if (!response.isSuccessful) {
                    throw IllegalStateException(
                        json.optString("detail").takeIf { it.isNotBlank() }
                            ?: "Authenticator request failed (HTTP ${response.code})"
                    )
                }
                json
            }
        }

}
