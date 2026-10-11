package com.atlas.trading.network

import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.withContext
import okhttp3.OkHttpClient
import okhttp3.Request
import org.json.JSONObject
import java.net.URLEncoder

/**
 * Customer-scoped dashboard data. Every endpoint requires the authenticated customer's bearer
 * token; missing/failed data is represented as unavailable, never filled with demo values.
 */
class CustomerDataClient(
    private val baseUrl: String,
    private val http: OkHttpClient = OkHttpClient()
) {
    data class Profile(val displayName: String?, val email: String?)
    data class Portfolio(
        val equity: Double?,
        val availableCash: Double?,
        val openPositions: Int?
    )
    data class MarketQuote(val symbol: String, val price: Double, val signal: String)
    data class Snapshot(
        val profile: Profile?,
        val portfolio: Portfolio?,
        val marketQuotes: List<MarketQuote>,
        val profileUnavailableReason: String?,
        val portfolioUnavailableReason: String?,
        val marketUnavailableReason: String?
    )

    suspend fun loadSnapshot(accessToken: String): Snapshot = withContext(Dispatchers.IO) {
        require(accessToken.isNotBlank()) { "Customer sign-in is required to load account data" }
        val profileResult = requestJson("/api/customer/me", accessToken)
        val portfolioResult = requestJson("/api/customer/portfolio", accessToken)
        val marketResult = requestJson(
            "/api/customer/market-scanner?exchange=binance&symbols=" +
                URLEncoder.encode("BTC/USDT:USDT,ETH/USDT:USDT", Charsets.UTF_8.name()),
            accessToken
        )

        val profile = profileResult.body?.let(::parseProfile)
        val portfolio = portfolioResult.body?.let(::parsePortfolio)
        val quotes = marketResult.body?.let(::parseMarketQuotes).orEmpty()
        Snapshot(
            profile = profile,
            portfolio = portfolio,
            marketQuotes = quotes,
            profileUnavailableReason = if (profile == null) profileResult.error ?: "Customer profile unavailable" else null,
            portfolioUnavailableReason = if (portfolio == null) portfolioResult.error ?: "Portfolio unavailable" else null,
            marketUnavailableReason = if (quotes.isEmpty()) marketResult.error ?: "Live market data unavailable" else null
        )
    }

    private data class JsonResult(val body: JSONObject?, val error: String?)

    private fun requestJson(path: String, token: String): JsonResult {
        return try {
            val request = Request.Builder()
                .url(baseUrl.trimEnd('/') + path)
                .get()
                .header("Accept", "application/json")
                .header("Authorization", "Bearer $token")
                .build()
            http.newCall(request).execute().use { response ->
                val raw = response.body?.string().orEmpty()
                if (!response.isSuccessful) {
                    JsonResult(null, "Unavailable (HTTP ${response.code})")
                } else {
                    val json = runCatching { JSONObject(raw) }.getOrNull()
                    if (json == null) JsonResult(null, "Unavailable (invalid server response)")
                    else JsonResult(json, null)
                }
            }
        } catch (_: Exception) {
            JsonResult(null, "Unavailable (server could not be reached)")
        }
    }

    companion object {
        internal fun parseProfile(json: JSONObject): Profile = Profile(
            displayName = json.optString("display_name").takeIf { it.isNotBlank() && it != "null" },
            email = json.optString("email").takeIf { it.isNotBlank() && it != "null" }
        )

        internal fun parsePortfolio(json: JSONObject): Portfolio {
            val positions = json.optJSONArray("positions")
            return Portfolio(
                equity = json.optionalFiniteDouble("equity"),
                availableCash = json.optionalFiniteDouble("cash_equity"),
                openPositions = positions?.length()
            )
        }

        internal fun parseMarketQuotes(json: JSONObject): List<MarketQuote> {
            val results = json.optJSONArray("results") ?: return emptyList()
            return buildList {
                for (index in 0 until results.length()) {
                    val row = results.optJSONObject(index) ?: continue
                    val symbol = row.optString("symbol").takeIf { it.isNotBlank() && it != "null" } ?: continue
                    val price = row.optionalFiniteDouble("price") ?: continue
                    val signal = row.optString("signal").takeIf { it.isNotBlank() && it != "null" } ?: "Unavailable"
                    if (signal != "ERROR") add(MarketQuote(symbol, price, signal))
                }
            }
        }

        private fun JSONObject.optionalFiniteDouble(key: String): Double? {
            if (!has(key) || isNull(key)) return null
            val value = optDouble(key, Double.NaN)
            return value.takeIf { it.isFinite() }
        }
    }
}
