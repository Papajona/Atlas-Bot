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
    data class RecentTrade(
        val id: Long,
        val symbol: String,
        val side: String,
        val mode: String,
        val status: String,
        val filledQuantity: String,
        val averageFillPrice: String,
        val createdAt: String?
    )
    data class EquityHistoryPoint(
        val observedAt: String,
        val equity: Double
    )
    data class Snapshot(
        val profile: Profile?,
        val portfolio: Portfolio?,
        val marketQuotes: List<MarketQuote>,
        val profileUnavailableReason: String?,
        val portfolioUnavailableReason: String?,
        val marketUnavailableReason: String?,
        val recentTrades: List<RecentTrade> = emptyList(),
        val recentTradesUnavailableReason: String? = null,
        val equityHistory: List<EquityHistoryPoint> = emptyList(),
        val equityHistoryUnavailableReason: String? = null,
        val planName: String? = null,
        val subscriptionStatus: String? = null
    )

    suspend fun loadSnapshot(
        accessToken: String,
        scannerAssetClass: String = "crypto"
    ): Snapshot = withContext(Dispatchers.IO) {
        require(accessToken.isNotBlank()) { "Customer sign-in is required to load account data" }
        val assetClass = scannerAssetClass.lowercase().takeIf {
            it in setOf("crypto", "forex", "commodity")
        } ?: "crypto"
        val (scannerExchange, scannerSymbols) = when (assetClass) {
            "forex" -> "yfinance" to "EUR_USD,GBP_USD,USD_JPY,AUD_USD,USD_CAD"
            "commodity" -> "oanda" to "XAU_USD,XAG_USD,WTICO_USD,BCO_USD,NATGAS_USD"
            else -> "binance" to "BTC/USDT:USDT,ETH/USDT:USDT"
        }
        val profileResult = requestJson("/api/customer/me", accessToken)
        val portfolioResult = requestJson("/api/customer/portfolio", accessToken)
        val marketResult = requestJson(
            "/api/customer/market-scanner?asset_class=$assetClass&exchange=$scannerExchange&symbols=" +
                URLEncoder.encode(scannerSymbols, Charsets.UTF_8.name()),
            accessToken
        )
        val recentTradesResult = requestJson("/api/customer/trades/recent?limit=20", accessToken)
        val billingResult = requestJson("/api/customer/billing", accessToken)
        val equityHistoryResult = requestJson(
            "/api/customer/portfolio/equity-history?days=30",
            accessToken
        )

        val profile = profileResult.body?.let(::parseProfile)
        val portfolio = portfolioResult.body?.let(::parsePortfolio)
        val quotes = marketResult.body?.let(::parseMarketQuotes).orEmpty()
        val recentTrades = recentTradesResult.body?.let(::parseRecentTrades).orEmpty()
        val equityHistory = equityHistoryResult.body?.let(::parseEquityHistory).orEmpty()
        Snapshot(
            profile = profile,
            portfolio = portfolio,
            marketQuotes = quotes,
            profileUnavailableReason = if (profile == null) profileResult.error ?: "Customer profile unavailable" else null,
            portfolioUnavailableReason = if (portfolio == null) portfolioResult.error ?: "Portfolio unavailable" else null,
            marketUnavailableReason = if (quotes.isEmpty()) marketResult.error ?: "Live market data unavailable" else null,
            recentTrades = recentTrades,
            recentTradesUnavailableReason = if (recentTradesResult.body == null) {
                recentTradesResult.error ?: "Recent trade history unavailable"
            } else null,
            equityHistory = equityHistory,
            equityHistoryUnavailableReason = when {
                equityHistoryResult.body == null -> equityHistoryResult.error ?: "Equity history unavailable"
                equityHistory.isEmpty() -> "No persisted equity observations have been recorded yet"
                else -> null
            },
            planName = billingResult.body?.optJSONObject("plan")
                ?.optString("name")?.takeIf { it.isNotBlank() && it != "null" },
            subscriptionStatus = billingResult.body?.optJSONObject("subscription")
                ?.optString("status")?.takeIf { it.isNotBlank() && it != "null" }
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

        internal fun parseEquityHistory(json: JSONObject): List<EquityHistoryPoint> {
            val rows = json.optJSONArray("points") ?: return emptyList()
            return buildList {
                for (index in 0 until rows.length()) {
                    val row = rows.optJSONObject(index) ?: continue
                    val observedAt = row.optString("observed_at")
                        .takeIf { it.isNotBlank() && it != "null" } ?: continue
                    val source = row.optString("valuation_source")
                    val currency = row.optString("currency")
                    val equity = row.optionalFiniteDouble("equity") ?: continue
                    if (source != "TRADING_ACCOUNT_EQUITY" || currency != "USDT") continue
                    if (runCatching { java.time.Instant.parse(observedAt) }.isFailure) continue
                    add(EquityHistoryPoint(observedAt = observedAt, equity = equity))
                }
            }.distinctBy { it.observedAt }.sortedBy { java.time.Instant.parse(it.observedAt) }
        }

        internal fun parseRecentTrades(json: JSONObject): List<RecentTrade> {
            val rows = json.optJSONArray("trades") ?: return emptyList()
            return buildList {
                for (index in 0 until rows.length()) {
                    val row = rows.optJSONObject(index) ?: continue
                    val id = row.optLong("id", 0L)
                    val symbol = row.optString("symbol").takeIf { it.isNotBlank() && it != "null" } ?: continue
                    if (id <= 0L) continue
                    add(
                        RecentTrade(
                            id = id,
                            symbol = symbol,
                            side = row.optString("side", "UNKNOWN").uppercase(),
                            mode = row.optString("mode", "UNKNOWN").uppercase(),
                            status = row.optString("status", "UNKNOWN").uppercase(),
                            filledQuantity = row.optString("filled_quantity", "0").takeIf { it != "null" } ?: "0",
                            averageFillPrice = row.optString("average_fill_price", "0").takeIf { it != "null" } ?: "0",
                            createdAt = row.optString("created_at").takeIf { it.isNotBlank() && it != "null" }
                        )
                    )
                }
            }
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
