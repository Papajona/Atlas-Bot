package com.atlas.trading.ui

import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Test

class RiskDashboardAuditTest {

    @Test
    fun zeroOrUnavailableTelemetryDoesNotClaimAConfiguredBudgetOrProtectedReserve() {
        val defaultStatus = resolveAiSafetyAuditStatus(PortfolioRiskMetrics())
        assertFalse(defaultStatus.hasValidatedDeadline)
        assertEquals("unavailable", defaultStatus.deadlineLabel)
        assertEquals("not independently attested", defaultStatus.vetoReserveStatus)

        val zeroDeadline = resolveAiSafetyAuditStatus(
            PortfolioRiskMetrics(aiSafetyTimeoutSeconds = 0.0, telemetryAvailable = true)
        )
        assertFalse(zeroDeadline.hasValidatedDeadline)
        assertEquals("unavailable", zeroDeadline.deadlineLabel)
        assertEquals("not independently attested", zeroDeadline.vetoReserveStatus)
    }

    @Test
    fun onlyPositiveFiniteLiveTelemetryReportsTheDecisionDeadline() {
        val status = resolveAiSafetyAuditStatus(
            PortfolioRiskMetrics(aiSafetyTimeoutSeconds = 1.5, telemetryAvailable = true)
        )
        assertTrue(status.hasValidatedDeadline)
        assertEquals("1.5s total (reported)", status.deadlineLabel)
        // A deadline value alone does not prove the Native AI veto reserve is protected.
        assertEquals("not independently attested", status.vetoReserveStatus)
    }

    @Test
    fun staleOrInvalidDeadlineValuesFailClosed() {
        val stale = resolveAiSafetyAuditStatus(
            PortfolioRiskMetrics(aiSafetyTimeoutSeconds = 2.0, telemetryAvailable = false)
        )
        assertFalse(stale.hasValidatedDeadline)
        assertEquals("unavailable", stale.deadlineLabel)

        listOf(Double.NaN, Double.POSITIVE_INFINITY, -1.0).forEach { invalid ->
            val status = resolveAiSafetyAuditStatus(
                PortfolioRiskMetrics(aiSafetyTimeoutSeconds = invalid, telemetryAvailable = true)
            )
            assertFalse(status.hasValidatedDeadline)
            assertEquals("unavailable", status.deadlineLabel)
        }
    }
}
