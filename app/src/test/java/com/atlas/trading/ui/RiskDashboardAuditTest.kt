package com.atlas.trading.ui

import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Test

class RiskDashboardAuditTest {

    @Test
    fun zeroOrUnavailableTelemetryDoesNotClaimAConfiguredBudgetOrProtectedReserve() {
        val now = 100_000L
        val defaultStatus = resolveAiSafetyAuditStatus(
            PortfolioRiskMetrics(),
            webSocketConnected = false,
            nowEpochMs = now
        )
        assertFalse(defaultStatus.hasValidatedDeadline)
        assertEquals("unavailable", defaultStatus.deadlineLabel)
        assertEquals("not independently attested", defaultStatus.vetoReserveStatus)

        val zeroDeadline = resolveAiSafetyAuditStatus(
            PortfolioRiskMetrics(
                aiSafetyTimeoutSeconds = 0.0,
                telemetryAvailable = true,
                lastUpdatedEpochMs = now
            ),
            webSocketConnected = true,
            nowEpochMs = now
        )
        assertFalse(zeroDeadline.hasValidatedDeadline)
        assertEquals("unavailable", zeroDeadline.deadlineLabel)
        assertEquals("not independently attested", zeroDeadline.vetoReserveStatus)
    }

    @Test
    fun onlyRecentPositiveFiniteConnectedTelemetryReportsTheDecisionDeadline() {
        val now = 100_000L
        val status = resolveAiSafetyAuditStatus(
            PortfolioRiskMetrics(
                aiSafetyTimeoutSeconds = 1.5,
                telemetryAvailable = true,
                lastUpdatedEpochMs = now - 20_000L
            ),
            webSocketConnected = true,
            nowEpochMs = now
        )
        assertTrue(status.hasValidatedDeadline)
        assertEquals("1.5s total (reported)", status.deadlineLabel)
        // A deadline value alone does not prove the Native AI veto reserve is protected.
        assertEquals("not independently attested", status.vetoReserveStatus)
    }

    @Test
    fun disconnectedOrExpiredTelemetryFailsClosed() {
        val now = 100_000L
        val recentMetrics = PortfolioRiskMetrics(
            aiSafetyTimeoutSeconds = 2.0,
            telemetryAvailable = true,
            lastUpdatedEpochMs = now - 1_000L
        )
        val disconnected = resolveAiSafetyAuditStatus(
            recentMetrics,
            webSocketConnected = false,
            nowEpochMs = now
        )
        assertFalse(disconnected.hasValidatedDeadline)
        assertEquals("unavailable", disconnected.deadlineLabel)

        val expired = resolveAiSafetyAuditStatus(
            recentMetrics.copy(lastUpdatedEpochMs = now - AI_SAFETY_TELEMETRY_MAX_AGE_MS - 1L),
            webSocketConnected = true,
            nowEpochMs = now
        )
        assertFalse(expired.hasValidatedDeadline)
        assertEquals("unavailable", expired.deadlineLabel)

        val futureDated = resolveAiSafetyAuditStatus(
            recentMetrics.copy(lastUpdatedEpochMs = now + AI_SAFETY_TELEMETRY_MAX_FUTURE_SKEW_MS + 1L),
            webSocketConnected = true,
            nowEpochMs = now
        )
        assertFalse(futureDated.hasValidatedDeadline)
        assertEquals("unavailable", futureDated.deadlineLabel)
    }

    @Test
    fun invalidDeadlineValuesFailClosedEvenWhenTheFrameIsFresh() {
        val now = 100_000L
        listOf(Double.NaN, Double.POSITIVE_INFINITY, -1.0).forEach { invalid ->
            val status = resolveAiSafetyAuditStatus(
                PortfolioRiskMetrics(
                    aiSafetyTimeoutSeconds = invalid,
                    telemetryAvailable = true,
                    lastUpdatedEpochMs = now
                ),
                webSocketConnected = true,
                nowEpochMs = now
            )
            assertFalse(status.hasValidatedDeadline)
            assertEquals("unavailable", status.deadlineLabel)
        }
    }
}
