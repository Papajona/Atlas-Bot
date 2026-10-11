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
    fun dashboardDoesNotFabricateHistoricalEquityData() {
        // Historical values must come from an authenticated customer API, never a generated demo curve.
        assertTrue(generate30DayEquityData().isEmpty())
    }
}
