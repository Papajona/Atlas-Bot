package com.atlas.trading.ui

import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Test

class RiskDashboardAuditTest {
    private val frameReceivedElapsedMs = 80_000L
    private val nowElapsedMs = 100_000L
    private val validMetrics = PortfolioRiskMetrics(
        aiSafetyTimeoutSeconds = 1.5,
        telemetryAvailable = true,
        lastUpdatedEpochMs = 1_800_000_000_000L
    )

    @Test
    fun zeroOrUnavailableTelemetryDoesNotClaimAConfiguredBudgetOrProtectedReserve() {
        val status = resolveAiSafetyAuditStatus(
            metrics = PortfolioRiskMetrics(),
            webSocketConnected = false,
            frameReceivedElapsedRealtimeMs = 0L,
            nowElapsedRealtimeMs = nowElapsedMs
        )
        assertFalse(status.hasValidatedDeadline)
        assertEquals("unavailable", status.deadlineLabel)
        assertEquals("not independently attested", status.vetoReserveStatus)

        val zeroDeadline = resolveAiSafetyAuditStatus(
            metrics = validMetrics.copy(aiSafetyTimeoutSeconds = 0.0),
            webSocketConnected = true,
            frameReceivedElapsedRealtimeMs = frameReceivedElapsedMs,
            nowElapsedRealtimeMs = nowElapsedMs
        )
        assertFalse(zeroDeadline.hasValidatedDeadline)
        assertEquals("unavailable", zeroDeadline.deadlineLabel)
    }

    @Test
    fun onlyPositiveFiniteConnectedTelemetryWithinMonotonicWindowIsReported() {
        val status = resolveAiSafetyAuditStatus(
            metrics = validMetrics,
            webSocketConnected = true,
            frameReceivedElapsedRealtimeMs = frameReceivedElapsedMs,
            nowElapsedRealtimeMs = nowElapsedMs
        )
        assertTrue(status.hasValidatedDeadline)
        assertEquals("1.5s total (reported)", status.deadlineLabel)
        assertEquals("not independently attested", status.vetoReserveStatus)
    }

    @Test
    fun disconnectedTelemetryFailsClosedEvenWhenTheLastFrameWasRecent() {
        val status = resolveAiSafetyAuditStatus(
            metrics = validMetrics,
            webSocketConnected = false,
            frameReceivedElapsedRealtimeMs = frameReceivedElapsedMs,
            nowElapsedRealtimeMs = nowElapsedMs
        )
        assertFalse(status.hasValidatedDeadline)
        assertEquals("unavailable", status.deadlineLabel)
    }

    @Test
    fun monotonicExpiryCannotBeExtendedByABackwardWallClockCorrection() {
        // Wall-clock changes are deliberately not inputs to the resolver. The
        // accepted frame expires as monotonic elapsed time passes the deadline.
        val atBoundary = resolveAiSafetyAuditStatus(
            metrics = validMetrics,
            webSocketConnected = true,
            frameReceivedElapsedRealtimeMs = frameReceivedElapsedMs,
            nowElapsedRealtimeMs = frameReceivedElapsedMs + AI_SAFETY_TELEMETRY_MAX_AGE_MS
        )
        assertTrue(atBoundary.hasValidatedDeadline)

        val afterBoundary = resolveAiSafetyAuditStatus(
            metrics = validMetrics,
            webSocketConnected = true,
            frameReceivedElapsedRealtimeMs = frameReceivedElapsedMs,
            nowElapsedRealtimeMs = frameReceivedElapsedMs + AI_SAFETY_TELEMETRY_MAX_AGE_MS + 1L
        )
        assertFalse(afterBoundary.hasValidatedDeadline)
        assertEquals("unavailable", afterBoundary.deadlineLabel)
    }

    @Test
    fun missingOrRegressedMonotonicReceiptTimeFailsClosed() {
        val missingReceipt = resolveAiSafetyAuditStatus(
            metrics = validMetrics,
            webSocketConnected = true,
            frameReceivedElapsedRealtimeMs = 0L,
            nowElapsedRealtimeMs = nowElapsedMs
        )
        assertFalse(missingReceipt.hasValidatedDeadline)

        val regressedClock = resolveAiSafetyAuditStatus(
            metrics = validMetrics,
            webSocketConnected = true,
            frameReceivedElapsedRealtimeMs = nowElapsedMs + 1L,
            nowElapsedRealtimeMs = nowElapsedMs
        )
        assertFalse(regressedClock.hasValidatedDeadline)
    }

    @Test
    fun invalidDeadlineValuesFailClosedEvenWhenTheFrameIsFresh() {
        listOf(Double.NaN, Double.POSITIVE_INFINITY, -1.0).forEach { invalid ->
            val status = resolveAiSafetyAuditStatus(
                metrics = validMetrics.copy(aiSafetyTimeoutSeconds = invalid),
                webSocketConnected = true,
                frameReceivedElapsedRealtimeMs = frameReceivedElapsedMs,
                nowElapsedRealtimeMs = nowElapsedMs
            )
            assertFalse(status.hasValidatedDeadline)
            assertEquals("unavailable", status.deadlineLabel)
        }
    }
}
