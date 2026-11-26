package com.atlas.trading.network

import org.json.JSONObject
import org.junit.Assert.assertEquals
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Test
import kotlinx.coroutines.runBlocking
import okhttp3.mockwebserver.MockResponse
import okhttp3.mockwebserver.MockWebServer

class CustomerDataClientTest {
    @Test
    fun profileUsesServerIdentityAndDoesNotInventAName() {
        val profile = CustomerDataClient.parseProfile(
            JSONObject("""{"display_name":"Sam K.","email":"sam@example.test"}""")
        )
        assertEquals("Sam K.", profile.displayName)
        assertEquals("sam@example.test", profile.email)

        val emptyProfile = CustomerDataClient.parseProfile(JSONObject("{}"))
        assertNull(emptyProfile.displayName)
        assertNull(emptyProfile.email)
    }

    @Test
    fun portfolioParsesVerifiedFieldsAndLeavesMissingMetricsUnavailable() {
        val portfolio = CustomerDataClient.parsePortfolio(
            JSONObject("""{"equity":125.75,"cash_equity":100.25,"positions":[{"symbol":"BTC/USDT"},{"symbol":"ETH/USDT"}]}""")
        )
        assertEquals(125.75, portfolio.equity!!, 0.000001)
        assertEquals(100.25, portfolio.availableCash!!, 0.000001)
        assertEquals(2, portfolio.openPositions)

        val missing = CustomerDataClient.parsePortfolio(JSONObject("{}"))
        assertNull(missing.equity)
        assertNull(missing.availableCash)
        assertNull(missing.openPositions)
    }

    @Test
    fun equityHistoryParserAcceptsOnlyVerifiedFiniteAccountEquityPoints() {
        val points = CustomerDataClient.parseEquityHistory(
            JSONObject(
                """{"points":[
                    {"observed_at":"2026-11-25T10:00:00+00:00","equity":125.5,"currency":"USDT","valuation_source":"TRADING_ACCOUNT_EQUITY"},
                    {"observed_at":"2026-11-25T10:05:00+00:00","equity":126.0,"currency":"USDT","valuation_source":"PAPER_LEDGER_CASH_ONLY"},
                    {"observed_at":"2026-11-25T10:15:00+00:00","equity":"NaN","currency":"USDT","valuation_source":"TRADING_ACCOUNT_EQUITY"},
                    {"observed_at":"2026-11-25T10:30:00+00:00","equity":140.0,"currency":"USD","valuation_source":"TRADING_ACCOUNT_EQUITY"},
                    {"observed_at":"2026-11-25T10:45:00+00:00","equity":150.0,"currency":"USDT","valuation_source":"UNVERIFIED"}
                ]}"""
            )
        )
        assertEquals(2, points.size)
        assertEquals("2026-11-25T10:00:00+00:00", points.first().observedAt)
        assertEquals(125.5, points.first().equity, 0.000001)
        assertEquals("2026-11-25T10:05:00+00:00", points.last().observedAt)
        assertEquals(126.0, points.last().equity, 0.000001)
    }

    @Test
    fun workspacePreferencesParserAppliesSafeChartDefaultsAndValidation() {
        val preferences = CustomerDataClient.parseWorkspacePreferences(
            JSONObject("""{"preferences":{"mobile_default_timeframe_days":7,"mobile_chart_crosshair_enabled":false}}""")
        )
        assertEquals(7, preferences.defaultChartWindowDays)
        assertEquals(false, preferences.chartCrosshairEnabled)

        val defaults = CustomerDataClient.parseWorkspacePreferences(
            JSONObject("""{"preferences":{}}""")
        )
        assertEquals(30, defaults.defaultChartWindowDays)
        assertEquals(true, defaults.chartCrosshairEnabled)
    }

    @Test
    fun chartWorkspacePreferencesAreLoadedAndSavedForAuthenticatedCustomer() = runBlocking {
        val server = MockWebServer()
        server.enqueue(MockResponse().setResponseCode(200).setBody(
            """{"scope":"customer_only","configured":true,"preferences":{"mobile_default_timeframe_days":7,"mobile_chart_crosshair_enabled":false}}"""
        ))
        server.enqueue(MockResponse().setResponseCode(200).setBody(
            """{"scope":"customer_only","configured":true,"preferences":{"mobile_default_timeframe_days":30,"mobile_chart_crosshair_enabled":true}}"""
        ))
        server.start()
        try {
            val client = CustomerDataClient(server.url("/").toString())
            val loaded = client.loadWorkspacePreferences("test-customer-token")
            assertEquals(7, loaded.defaultChartWindowDays)
            assertEquals(false, loaded.chartCrosshairEnabled)

            val saved = client.updateWorkspacePreferences(
                "test-customer-token",
                defaultChartWindowDays = 30,
                chartCrosshairEnabled = true
            )
            assertEquals(30, saved.defaultChartWindowDays)
            assertEquals(true, saved.chartCrosshairEnabled)

            val getRequest = server.takeRequest()
            assertEquals("GET", getRequest.method)
            assertEquals("/api/customer/workspace-preferences", getRequest.path)
            assertEquals("Bearer test-customer-token", getRequest.getHeader("Authorization"))

            val patchRequest = server.takeRequest()
            assertEquals("PATCH", patchRequest.method)
            assertEquals("/api/customer/workspace-preferences", patchRequest.path)
            assertEquals("Bearer test-customer-token", patchRequest.getHeader("Authorization"))
            val patchBody = JSONObject(patchRequest.body.readUtf8())
            assertEquals(30, patchBody.getInt("mobile_default_timeframe_days"))
            assertTrue(patchBody.getBoolean("mobile_chart_crosshair_enabled"))
        } finally {
            server.shutdown()
        }
    }

    @Test
    fun marketParserDropsErrorsAndInvalidOrNonFinitePrices() {
        val quotes = CustomerDataClient.parseMarketQuotes(
            JSONObject(
                """{"results":[
                    {"symbol":"BTC/USDT","price":65000.5,"signal":"WATCH"},
                    {"symbol":"ETH/USDT","price":"NaN","signal":"LONG"},
                    {"symbol":"BAD/USDT","signal":"ERROR","error":"provider unavailable"},
                    {"symbol":"XRP/USDT","price":0.52,"signal":"SHORT"}
                ]}"""
            )
        )
        assertEquals(2, quotes.size)
        assertEquals("BTC/USDT", quotes[0].symbol)
        assertEquals(65000.5, quotes[0].price, 0.000001)
        assertEquals("XRP/USDT", quotes[1].symbol)
    }

    @Test
    fun marketParserReturnsEmptyWhenFeedIsUnavailable() {
        assertTrue(CustomerDataClient.parseMarketQuotes(JSONObject("{}")).isEmpty())
    }
    @Test
    fun authenticatedSnapshotUsesCustomerScopedEndpointsAndParsesLiveResponses() = runBlocking {
        val server = MockWebServer()
        server.enqueue(MockResponse().setResponseCode(200).setBody(
            """{"display_name":"Verified Customer","email":"customer@example.test"}"""
        ))
        server.enqueue(MockResponse().setResponseCode(200).setBody(
            """{"equity":912.34,"cash_equity":800.12,"positions":[{"symbol":"BTC/USDT"}]}"""
        ))
        server.enqueue(MockResponse().setResponseCode(200).setBody(
            """{"results":[{"symbol":"BTC/USDT","price":65000.25,"signal":"WATCH"}]}"""
        ))
        server.enqueue(MockResponse().setResponseCode(200).setBody("""{"trades":[]}"""))
        server.enqueue(MockResponse().setResponseCode(200).setBody(
            """{"plan":{"name":"Free"},"subscription":{"status":"active"}}"""
        ))
        server.enqueue(MockResponse().setResponseCode(200).setBody(
            """{"points":[{"observed_at":"2026-11-25T10:00:00+00:00","equity":912.34,"cash_equity":800.12,"currency":"USDT","valuation_source":"TRADING_ACCOUNT_EQUITY"}]}"""
        ))
        server.start()
        try {
            val snapshot = CustomerDataClient(server.url("/").toString()).loadSnapshot("test-customer-token")
            assertEquals("Verified Customer", snapshot.profile?.displayName)
            assertEquals(912.34, snapshot.portfolio?.equity!!, 0.000001)
            assertEquals(1, snapshot.portfolio?.openPositions)
            assertEquals("BTC/USDT", snapshot.marketQuotes.single().symbol)
            assertEquals(65000.25, snapshot.marketQuotes.single().price, 0.000001)
            assertEquals(912.34, snapshot.equityHistory.single().equity, 0.000001)
            assertNull(snapshot.equityHistoryUnavailableReason)
            assertNull(snapshot.profileUnavailableReason)
            assertNull(snapshot.portfolioUnavailableReason)
            assertNull(snapshot.marketUnavailableReason)

            repeat(6) {
                val request = server.takeRequest()
                assertEquals("Bearer test-customer-token", request.getHeader("Authorization"))
                assertEquals("GET", request.method)
            }
        } finally {
            server.shutdown()
        }
    }

    @Test
    fun scannerRequestSelectsForexMarketData() = runBlocking {
        val server = MockWebServer()
        repeat(6) { server.enqueue(MockResponse().setResponseCode(200).setBody("{}")) }
        server.start()
        try {
            CustomerDataClient(server.url("/").toString())
                .loadSnapshot("test-customer-token", scannerAssetClass = "forex")
            val paths = (0 until 6).map { server.takeRequest().path.orEmpty() }
            assertTrue(paths[2].contains("asset_class=forex"))
            assertTrue(paths[2].contains("exchange=yfinance"))
            assertTrue(paths[2].contains("symbols=EUR_USD%2CGBP_USD"))
        } finally {
            server.shutdown()
        }
    }

    @Test
    fun scannerRequestSelectsPracticeCommodityMarketData() = runBlocking {
        val server = MockWebServer()
        repeat(6) { server.enqueue(MockResponse().setResponseCode(200).setBody("{}")) }
        server.start()
        try {
            CustomerDataClient(server.url("/").toString())
                .loadSnapshot("test-customer-token", scannerAssetClass = "commodity")
            val paths = (0 until 6).map { server.takeRequest().path.orEmpty() }
            assertTrue(paths[2].contains("asset_class=commodity"))
            assertTrue(paths[2].contains("exchange=oanda"))
            assertTrue(paths[2].contains("symbols=XAU_USD%2CXAG_USD"))
        } finally {
            server.shutdown()
        }
    }

    @Test
    fun failedCustomerAndMarketRequestsStayExplicitlyUnavailable() = runBlocking {
        val server = MockWebServer()
        server.enqueue(MockResponse().setResponseCode(401).setBody("""{"detail":"Unauthorized"}"""))
        server.enqueue(MockResponse().setResponseCode(403).setBody("""{"detail":"Feature not enabled"}"""))
        server.enqueue(MockResponse().setResponseCode(403).setBody("""{"detail":"Feature not enabled"}"""))
        server.enqueue(MockResponse().setResponseCode(404).setBody("""{"detail":"Not found"}"""))
        server.enqueue(MockResponse().setResponseCode(404).setBody("""{"detail":"Not found"}"""))
        server.enqueue(MockResponse().setResponseCode(403).setBody("""{"detail":"Feature not enabled"}"""))
        server.start()
        try {
            val snapshot = CustomerDataClient(server.url("/").toString()).loadSnapshot("expired-token")
            assertNull(snapshot.profile)
            assertNull(snapshot.portfolio)
            assertTrue(snapshot.marketQuotes.isEmpty())
            assertTrue(snapshot.profileUnavailableReason!!.contains("HTTP 401"))
            assertTrue(snapshot.portfolioUnavailableReason!!.contains("HTTP 403"))
            assertTrue(snapshot.marketUnavailableReason!!.contains("HTTP 403"))
            assertTrue(snapshot.equityHistoryUnavailableReason!!.contains("HTTP 403"))
        } finally {
            server.shutdown()
        }
    }

}
