package com.atlas.trading.network

import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.withContext
import okhttp3.MediaType.Companion.toMediaType
import okhttp3.OkHttpClient
import okhttp3.Request
import okhttp3.RequestBody.Companion.toRequestBody
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
    data class WorkspacePreferences(
        val defaultChartWindowDays: Int,
        val chartCrosshairEnabled: Boolean
    )
    data class PaperOrder(
        val clientOrderId: String,
        val symbol: String,
        val side: String,
        val status: String,
        val orderType: String,
        val quantity: String,
        val filledQuantity: String,
        val remainingQuantity: String,
        val averageFillPrice: String?
    )
    data class PaperOrderActionResult(
        val status: String,
        val fillNote: String?,
        val order: PaperOrder?
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

    suspend fun loadWorkspacePreferences(accessToken: String): WorkspacePreferences =
        withContext(Dispatchers.IO) {
            require(accessToken.isNotBlank()) { "Customer sign-in is required to load preferences" }
            val result = requestJson("/api/customer/workspace-preferences", accessToken)
            val body = result.body ?: throw IllegalStateException(
                result.error ?: "Workspace preferences are unavailable"
            )
            parseWorkspacePreferences(body)
        }

    suspend fun updateWorkspacePreferences(
        accessToken: String,
        defaultChartWindowDays: Int? = null,
        chartCrosshairEnabled: Boolean? = null
    ): WorkspacePreferences = withContext(Dispatchers.IO) {
        require(accessToken.isNotBlank()) { "Customer sign-in is required to save preferences" }
        require(defaultChartWindowDays == null || defaultChartWindowDays in setOf(7, 30)) {
            "Default chart window must be 7 or 30 days"
        }
        require(defaultChartWindowDays != null || chartCrosshairEnabled != null) {
            "At least one chart preference must be provided"
        }
        val payload = JSONObject()
        defaultChartWindowDays?.let { payload.put("mobile_default_timeframe_days", it) }
        chartCrosshairEnabled?.let { payload.put("mobile_chart_crosshair_enabled", it) }
        val result = requestPatchJson(
            "/api/customer/workspace-preferences",
            accessToken,
            payload
        )
        val body = result.body ?: throw IllegalStateException(
            result.error ?: "Workspace preferences could not be saved"
        )
        parseWorkspacePreferences(body)
    }

    suspend fun loadOpenPaperOrders(accessToken: String): List<PaperOrder> =
        withContext(Dispatchers.IO) {
            require(accessToken.isNotBlank()) { "Customer sign-in is required to load paper orders" }
            val result = requestJson("/api/customer/paper/orders", accessToken)
            val body = result.body ?: throw IllegalStateException(
                result.error ?: "Paper orders are unavailable"
            )
            parsePaperOrders(body)
        }

    suspend fun submitPaperOrder(
        accessToken: String,
        symbol: String,
        side: String,
        quantity: String,
        orderType: String = "MARKET",
        limitPrice: String? = null,
        timeframe: String = "1h"
    ): PaperOrderActionResult = withContext(Dispatchers.IO) {
        require(accessToken.isNotBlank()) { "Customer sign-in is required to submit a paper order" }
        require(symbol in setOf("BTC/USDT", "ETH/USDT")) {
            "The mobile paper order ticket supports BTC/USDT and ETH/USDT only"
        }
        require(side in setOf("BUY", "SELL")) { "Paper order side must be BUY or SELL" }
        val parsedQuantity = quantity.toBigDecimalOrNull()
        require(parsedQuantity != null && parsedQuantity.signum() > 0) {
            "Paper order quantity must be a positive decimal"
        }
        require(orderType in setOf("MARKET", "LIMIT")) { "Paper order type must be MARKET or LIMIT" }
        val parsedLimitPrice = limitPrice?.takeIf { it.isNotBlank() }?.toBigDecimalOrNull()
        if (orderType == "LIMIT") {
            require(parsedLimitPrice != null && parsedLimitPrice.signum() > 0) {
                "A positive limit price is required for a LIMIT paper order"
            }
        } else {
            require(limitPrice.isNullOrBlank()) { "A MARKET paper order must not include a limit price" }
        }
        require(timeframe in setOf("1m", "5m", "15m", "30m", "1h", "2h", "4h", "1d")) {
            "Paper order timeframe is not supported"
        }

        val payload = JSONObject()
            .put("client_order_id", "android-paper-" + java.util.UUID.randomUUID().toString())
            .put("symbol", symbol)
            .put("side", side)
            .put("quantity", parsedQuantity.stripTrailingZeros().toPlainString())
            .put("order_type", orderType)
            .put("timeframe", timeframe)
        if (orderType == "LIMIT") {
            payload.put("limit_price", parsedLimitPrice!!.stripTrailingZeros().toPlainString())
        }
        val result = requestPostJson("/api/customer/paper/orders", accessToken, payload)
        val body = result.body ?: throw IllegalStateException(
            result.error ?: "Paper order submission failed"
        )
        if (!body.optBoolean("virtual_only", false)) {
            throw IllegalStateException("Server did not confirm that this order is virtual-only")
        }
        PaperOrderActionResult(
            status = body.optString("status", "SUBMITTED").takeIf { it.isNotBlank() && it != "null" }
                ?: "SUBMITTED",
            fillNote = body.optString("fill_note").takeIf { it.isNotBlank() && it != "null" },
            order = body.optJSONObject("order")?.let(::parsePaperOrder)
        )
    }

    suspend fun advancePaperOrder(accessToken: String, clientOrderId: String): PaperOrderActionResult =
        withContext(Dispatchers.IO) {
            require(accessToken.isNotBlank()) { "Customer sign-in is required to advance a paper order" }
            require(clientOrderId.isNotBlank()) { "Paper order ID is required" }
            val result = requestPostJson(
                "/api/customer/paper/orders/${URLEncoder.encode(clientOrderId, Charsets.UTF_8.name())}/advance",
                accessToken,
                JSONObject()
            )
            parsePaperOrderAction(result, "Paper order status is unavailable")
        }

    suspend fun cancelPaperOrder(accessToken: String, clientOrderId: String): PaperOrderActionResult =
        withContext(Dispatchers.IO) {
            require(accessToken.isNotBlank()) { "Customer sign-in is required to cancel a paper order" }
            require(clientOrderId.isNotBlank()) { "Paper order ID is required" }
            val payload = JSONObject().put("idempotency_key", java.util.UUID.randomUUID().toString())
            val result = requestPostJson(
                "/api/customer/paper/orders/${URLEncoder.encode(clientOrderId, Charsets.UTF_8.name())}/cancel",
                accessToken,
                payload
            )
            parsePaperOrderAction(result, "Paper order cancellation failed")
        }

    private fun parsePaperOrderAction(result: JsonResult, fallback: String): PaperOrderActionResult {
        val body = result.body ?: throw IllegalStateException(result.error ?: fallback)
        if (!body.optBoolean("virtual_only", false)) {
            throw IllegalStateException("Server did not confirm that this order remains virtual-only")
        }
        return PaperOrderActionResult(
            status = body.optString("status", "UNKNOWN"),
            fillNote = body.optString("fill_note").takeIf { it.isNotBlank() && it != "null" },
            order = body.optJSONObject("order")?.let(::parsePaperOrder)
        )
    }

    private fun requestPostJson(path: String, token: String, payload: JSONObject): JsonResult {
        return try {
            val requestBody = payload.toString().toRequestBody(
                "application/json; charset=utf-8".toMediaType()
            )
            val request = Request.Builder()
                .url(baseUrl.trimEnd('/') + path)
                .post(requestBody)
                .header("Accept", "application/json")
                .header("Authorization", "Bearer $token")
                .header("Content-Type", "application/json")
                .build()
            http.newCall(request).execute().use { response ->
                val raw = response.body?.string().orEmpty()
                val json = runCatching { JSONObject(raw) }.getOrNull()
                if (!response.isSuccessful) {
                    val detail = json?.optString("detail")?.takeIf { it.isNotBlank() && it != "null" }
                    JsonResult(null, detail ?: "Unavailable (HTTP ${response.code})")
                } else {
                    if (json == null) JsonResult(null, "Unavailable (invalid server response)")
                    else JsonResult(json, null)
                }
            }
        } catch (_: Exception) {
            JsonResult(null, "Unavailable (server could not be reached)")
        }
    }

    private fun requestPatchJson(path: String, token: String, payload: JSONObject): JsonResult {
        return try {
            val requestBody = payload.toString().toRequestBody(
                "application/json; charset=utf-8".toMediaType()
            )
            val request = Request.Builder()
                .url(baseUrl.trimEnd('/') + path)
                .patch(requestBody)
                .header("Accept", "application/json")
                .header("Authorization", "Bearer $token")
                .header("Content-Type", "application/json")
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
        internal fun parsePaperOrders(json: JSONObject): List<PaperOrder> {
            val rows = json.optJSONArray("orders") ?: return emptyList()
            return buildList {
                for (index in 0 until rows.length()) {
                    val row = rows.optJSONObject(index) ?: continue
                    val id = row.optString("client_order_id")
                        .takeIf { it.isNotBlank() && it != "null" } ?: continue
                    val symbol = row.optString("symbol")
                        .takeIf { it.isNotBlank() && it != "null" } ?: continue
                    add(parsePaperOrder(row))
                }
            }
        }

        internal fun parsePaperOrder(json: JSONObject): PaperOrder {
            fun value(key: String, fallback: String = "0") =
                json.optString(key, fallback).takeIf { it.isNotBlank() && it != "null" } ?: fallback
            return PaperOrder(
                clientOrderId = value("client_order_id", ""),
                symbol = value("symbol", "—"),
                side = value("side", "—").uppercase(),
                status = value("status", "UNKNOWN").uppercase(),
                orderType = value("order_type", "MARKET").uppercase(),
                quantity = value("quantity"),
                filledQuantity = value("filled_quantity"),
                remainingQuantity = value("remaining_quantity"),
                averageFillPrice = value("average_fill_price").takeIf { it != "0" }
            )
        }

        internal fun parseWorkspacePreferences(json: JSONObject): WorkspacePreferences {
            val preferences = json.optJSONObject("preferences")
                ?: throw IllegalArgumentException("Workspace preferences response is invalid")
            val days = preferences.optInt("mobile_default_timeframe_days", 30)
            if (days !in setOf(7, 30)) {
                throw IllegalArgumentException("Server returned an unsupported chart window")
            }
            return WorkspacePreferences(
                defaultChartWindowDays = days,
                chartCrosshairEnabled = preferences.optBoolean(
                    "mobile_chart_crosshair_enabled",
                    true
                )
            )
        }

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
                    if (source !in setOf("TRADING_ACCOUNT_EQUITY", "PAPER_LEDGER_CASH_ONLY") || currency != "USDT") continue
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
