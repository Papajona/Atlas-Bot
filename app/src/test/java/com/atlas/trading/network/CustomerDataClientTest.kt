package com.atlas.trading.network

import org.json.JSONObject
import org.junit.Assert.assertEquals
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Test

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
}
