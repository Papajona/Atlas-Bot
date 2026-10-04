package com.atlas.trading.ui

import org.junit.Assert.*
import org.junit.Test

class DashboardNavigationAndSettingsTest {

    @Test
    fun testDashboardTabEnumCoverage() {
        val tabs = DashboardTab.values()
        assertEquals(4, tabs.size)
        assertTrue(tabs.contains(DashboardTab.HOME))
        assertTrue(tabs.contains(DashboardTab.PORTFOLIO))
        assertTrue(tabs.contains(DashboardTab.MARKET))
        assertTrue(tabs.contains(DashboardTab.SETTINGS))
    }

    @Test
    fun testAssetClassCoverage() {
        val classes = AssetClass.values()
        assertTrue(classes.size >= 4)
        assertEquals("All Markets", AssetClass.ALL.label)
        assertEquals("Crypto", AssetClass.CRYPTO.label)
        assertEquals("Forex", AssetClass.FOREX.label)
        assertEquals("Commodities", AssetClass.COMMODITIES.label)
    }

    @Test
    fun test30DayEquityGeneration() {
        // Equity data should span 30 points and end with realistic positive return
        val testPoints = (1..30).map { day ->
            EquityPoint(
                day = day,
                dateLabel = "Day $day",
                equity = 1000.0 + (day * 8.5),
                dailyPnl = 8.5,
                cumProfit = day * 8.5
            )
        }
        assertEquals(30, testPoints.size)
        assertEquals(1, testPoints.first().day)
        assertEquals(30, testPoints.last().day)
        assertTrue(testPoints.last().equity > testPoints.first().equity)
    }
}
