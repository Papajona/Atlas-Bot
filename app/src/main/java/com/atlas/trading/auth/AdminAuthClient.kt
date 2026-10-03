package com.atlas.trading.auth

import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.withContext
import okhttp3.MediaType.Companion.toMediaType
import okhttp3.OkHttpClient
import okhttp3.Request
import okhttp3.RequestBody.Companion.toRequestBody
import org.json.JSONObject

class AdminAuthClient(private val baseUrl: String) {
    private val client = OkHttpClient()
    private val jsonType = "application/json".toMediaType()
    private var pendingAccessToken: String? = null
    private var pendingFactorId: String? = null
    private var pendingChallengeId: String? = null

    data class MfaChallenge(val factorId: String, val challengeId: String)
    data class LoginResult(
        val accessToken: String?,
        val challenge: MfaChallenge?,
        val message: String
    )
    data class RiskState(val confirmed: Boolean, val halted: Boolean, val raw: JSONObject)

    suspend fun login(email: String, password: String): LoginResult = withContext(Dispatchers.IO) {
        require(email.isNotBlank() && password.isNotBlank()) { "Email and password are required" }
        val response = postJson(
            "/api/admin/auth/login",
            JSONObject().put("email", email.trim()).put("password", password)
        )
        if (!response.ok) throw IllegalStateException(response.body.optString("detail", "Administrator login failed"))
        val body = response.body
        val token = body.optString("access_token").takeIf { it.isNotBlank() }
        pendingAccessToken = token
        if (!body.optBoolean("mfa_required", false)) {
            return@withContext LoginResult(token, null, "Administrator session active")
        }
        if (!body.optBoolean("totp_enrolled", false)) {
            throw IllegalStateException("Administrator MFA is required but no verified authenticator is enrolled")
        }
        val status = authorizedGet("/api/admin/auth/mfa/status", token ?: throw IllegalStateException("Missing administrator session"))
        val factors = status.body.optJSONArray("factor_ids")
        val factorId = factors?.optString(0)?.takeIf { it.isNotBlank() }
            ?: throw IllegalStateException("No verified administrator authenticator factor is available")
        val challenge = postJson(
            "/api/admin/auth/mfa/challenge",
            JSONObject().put("factor_id", factorId),
            token
        )
        if (!challenge.ok) throw IllegalStateException(challenge.body.optString("detail", "Unable to start MFA challenge"))
        val challengeId = challenge.body.optString("id").takeIf { it.isNotBlank() }
            ?: throw IllegalStateException("MFA challenge did not return a challenge id")
        pendingFactorId = factorId
        pendingChallengeId = challengeId
        LoginResult(null, MfaChallenge(factorId, challengeId), "Enter your 6-digit authenticator code")
    }

    suspend fun verifyMfa(code: String): String = withContext(Dispatchers.IO) {
        require(code.matches(Regex("^\\d{6}$"))) { "Authenticator code must contain 6 digits" }
        val token = pendingAccessToken ?: throw IllegalStateException("Administrator login session expired; sign in again")
        val factor = pendingFactorId ?: throw IllegalStateException("MFA factor is missing; sign in again")
        val challenge = pendingChallengeId ?: throw IllegalStateException("MFA challenge is missing; sign in again")
        val response = postJson(
            "/api/admin/auth/mfa/verify",
            JSONObject().put("factor_id", factor).put("challenge_id", challenge).put("code", code),
            token
        )
        if (!response.ok) throw IllegalStateException(response.body.optString("detail", "Authenticator verification failed"))
        val verifiedToken = response.body.optString("access_token").takeIf { it.isNotBlank() }
            ?: throw IllegalStateException("MFA verification did not return an AAL2 access token")
        pendingAccessToken = null
        pendingFactorId = null
        pendingChallengeId = null
        verifiedToken
    }

    suspend fun setKillSwitch(halted: Boolean, accessToken: String?): RiskState = withContext(Dispatchers.IO) {
        val token = accessToken?.takeIf { it.isNotBlank() } ?: throw IllegalStateException("Authenticated administrator session required")
        val path = if (halted) "/api/risk/kill" else "/api/risk/reset"
        val response = postJson(path, JSONObject(), token)
        if (!response.ok) throw IllegalStateException(response.body.optString("detail", "Risk state request was rejected"))
        val body = response.body
        val confirmed = body.optBoolean("ok", false) &&
            body.has("kill_switch") &&
            body.optBoolean("kill_switch", !halted) == halted &&
            body.optBoolean("live_enabled", halted)
                .let { live -> if (halted) !live else live == false }
        RiskState(confirmed, body.optBoolean("kill_switch", !halted), body)
    }

    private data class HttpResult(val ok: Boolean, val body: JSONObject)

    private fun postJson(path: String, body: JSONObject, bearer: String? = null): HttpResult {
        val builder = Request.Builder()
            .url(baseUrl.trimEnd('/') + path)
            .post(body.toString().toRequestBody(jsonType))
            .header("Accept", "application/json")
            .header("Content-Type", "application/json")
        if (!bearer.isNullOrBlank()) builder.header("Authorization", "Bearer $bearer")
        return execute(builder.build())
    }

    private fun authorizedGet(path: String, bearer: String): HttpResult {
        val request = Request.Builder()
            .url(baseUrl.trimEnd('/') + path)
            .get()
            .header("Accept", "application/json")
            .header("Authorization", "Bearer $bearer")
            .build()
        return execute(request)
    }

    private fun execute(request: Request): HttpResult {
        client.newCall(request).execute().use { response ->
            val raw = response.body?.string().orEmpty()
            val body = runCatching { JSONObject(raw) }.getOrElse { JSONObject().put("detail", raw.ifBlank { "Empty server response" }) }
            return HttpResult(response.isSuccessful, body)
        }
    }
}
