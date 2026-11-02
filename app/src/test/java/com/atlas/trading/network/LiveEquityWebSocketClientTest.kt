package com.atlas.trading.network

import com.atlas.trading.ui.PortfolioRiskMetrics
import org.junit.Assert.*
import org.junit.Test

class LiveEquityWebSocketClientTest {

    @Test
    fun testBuildWebSocketUrlHttps() {
        val result = WebSocketManager.buildWebSocketUrl("https://ais-dev.run.app")
        assertEquals("wss://ais-dev.run.app/ws/risk-telemetry", result)
    }

    @Test
    fun testBuildWebSocketUrlHttp() {
        val result = WebSocketManager.buildWebSocketUrl("http://localhost:8000")
        assertEquals("ws://localhost:8000/ws/risk-telemetry", result)
    }

    @Test
    fun testBuildWebSocketUrlHttpIsRejectedWhenInsecureIsDisallowed() {
        val error = assertThrows(IllegalArgumentException::class.java) {
            WebSocketManager.buildWebSocketUrl("http://localhost:8000", allowInsecure = false)
        }
        assertTrue(error.message!!.contains("Cleartext WebSocket URLs are forbidden"))
    }

    @Test
    fun testBuildWebSocketUrlRawWsIsRejectedWhenInsecureIsDisallowed() {
        assertThrows(IllegalArgumentException::class.java) {
            WebSocketManager.buildWebSocketUrl("ws://localhost:8000", allowInsecure = false)
        }
    }

    @Test
    fun testBuildWebSocketUrlUnsupportedSchemeIsRejected() {
        assertThrows(IllegalArgumentException::class.java) {
            WebSocketManager.buildWebSocketUrl("ftp://example.invalid", allowInsecure = false)
        }
    }

    @Test
    fun testBuildWebSocketUrlPreservesExistingWsPath() {
        val result = WebSocketManager.buildWebSocketUrl("wss://ais-dev.run.app/ws/equity")
        assertEquals("wss://ais-dev.run.app/ws/equity", result)
    }

    @Test
    fun testParseEquityMessageValidPayload() {
        val jsonPayload = """
            {
                "equity": 10450.75,
                "peak_equity": 10500.00,
                "realized_pnl": 520.30,
                "unrealized_pnl": 112.45,
                "current_drawdown_pct": 0.47,
                "max_drawdown_limit_pct": 15.0,
                "daily_loss_pct": 0.12,
                "daily_loss_limit_pct": 3.0,
                "sharpe_ratio": 2.15,
                "calmar_ratio": 2.80,
                "sortino_ratio": 3.05,
                "deflated_sharpe_ratio": 0.99,
                "edge_decay_z_score": 1.12,
                "regime": "TRENDING MOMENTUM",
                "regime_description": "Strong trend persistence",
                "momentum_weight": 50,
                "mean_rev_weight": 10,
                "session_weight": 25,
                "carry_weight": 15,
                "cost_stress_headroom": 2.65,
                "ai_safety_timeout_seconds": 1.5,
                "is_halted": false,
                "stream_type": "WEBSOCKET_PUSH",
                "last_updated_epoch_ms": 1720000000000
            }
        """.trimIndent()

        val nowEpochMs = 1720000000000L
        val parsed = WebSocketManager.parseEquityMessage(
            jsonPayload, PortfolioRiskMetrics(), nowEpochMs = nowEpochMs
        )
        assertNotNull(parsed)
        parsed?.let {
            assertEquals(10450.75, it.equity, 0.001)
            assertEquals(10500.00, it.peakEquity, 0.001)
            assertEquals(520.30, it.realizedPnl, 0.001)
            assertEquals(112.45, it.unrealizedPnl, 0.001)
            assertEquals(0.47, it.currentDrawdownPct, 0.001)
            assertEquals(2.15, it.sharpeRatio, 0.001)
            assertEquals("TRENDING MOMENTUM", it.regime)
            assertEquals("WEBSOCKET_PUSH", it.streamType)
            assertFalse(it.isHalted)
            assertTrue(it.telemetryAvailable)
            assertEquals(1.5, it.aiSafetyTimeoutSeconds, 0.001)
            assertEquals(1720000000000L, it.lastUpdatedEpochMs)
        }
    }

    @Test
    fun testParseEquityMessageWithoutSafetyDeadlineDoesNotClaimLiveTelemetry() {
        val jsonPayload = """
            {
                "equity": 10450.75,
                "last_updated_epoch_ms": 1720000000000
            }
        """.trimIndent()

        val parsed = WebSocketManager.parseEquityMessage(
            jsonPayload, PortfolioRiskMetrics(), nowEpochMs = 1720000000000L
        )

        assertNotNull(parsed)
        assertFalse(parsed!!.telemetryAvailable)
    }

    @Test
    fun testParseEquityMessageRejectsStaleFutureAndInvalidServerTimestamps() {
        val nowEpochMs = 1720000000000L
        fun payload(timestamp: Long) = """
            {
                "equity": 10450.75,
                "ai_safety_timeout_seconds": 1.5,
                "last_updated_epoch_ms": $timestamp
            }
        """.trimIndent()

        assertNull(
            WebSocketManager.parseEquityMessage(
                payload(nowEpochMs - 30_001L), PortfolioRiskMetrics(), nowEpochMs
            )
        )
        assertNull(
            WebSocketManager.parseEquityMessage(
                payload(nowEpochMs + 5_001L), PortfolioRiskMetrics(), nowEpochMs
            )
        )
        assertNull(
            WebSocketManager.parseEquityMessage(
                payload(0L), PortfolioRiskMetrics(), nowEpochMs
            )
        )
        assertNull(
            WebSocketManager.parseEquityMessage(
                payload(-1L), PortfolioRiskMetrics(), nowEpochMs
            )
        )
    }

    @Test
    fun testParseEquityMessageRejectsMissingTimestamp() {
        val payload = """{"equity":10450.75,"ai_safety_timeout_seconds":1.5}"""
        assertNull(
            WebSocketManager.parseEquityMessage(
                payload, PortfolioRiskMetrics(), nowEpochMs = 1720000000000L
            )
        )
    }

    @Test
    fun testReconnectBackoffIsBounded() {
        assertEquals(1_000L, WebSocketManager.reconnectDelayMs(1))
        assertEquals(2_000L, WebSocketManager.reconnectDelayMs(2))
        assertEquals(8_000L, WebSocketManager.reconnectDelayMs(4))
        assertEquals(15_000L, WebSocketManager.reconnectDelayMs(5))
        assertEquals(15_000L, WebSocketManager.reconnectDelayMs(99))
    }

    @Test
    fun testParseEquityMessageInvalidPayloadReturnsNull() {
        val nonEquityPayload = """{"status": "ok", "message": "heartbeat"}"""
        val parsed = WebSocketManager.parseEquityMessage(nonEquityPayload, PortfolioRiskMetrics())
        assertNull(parsed)

        val malformedJson = "{ corrupted json string }"
        val malformedParsed = WebSocketManager.parseEquityMessage(malformedJson, PortfolioRiskMetrics())
        assertNull(malformedParsed)
    }
}
