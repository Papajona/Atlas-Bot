package com.atlas.trading.ui

import kotlinx.coroutines.runBlocking
import org.junit.Assert.*
import org.junit.Test

class StrategyReplayEngineTest {

    @Test
    fun testTrendingBullReplaySimulation() = runBlocking {
        val result = ReplaySimulator.runSimulation("Crypto BTC/USDT", "Trending Bull", 10000.0)

        println("=== REPLAY TEST: TRENDING BULL ===")
        println("Trades: ${result.totalTrades}, Wins: ${result.winningTrades}, Losses: ${result.losingTrades}")
        println("Win Rate: ${result.winRatePct}%, Net Return: ${result.netReturnPct}%, Net PnL: $${result.netProfit}")
        println("Max DD: ${result.maxDrawdownPct}%, Sharpe: ${result.sharpeRatio}, Profit Factor: ${result.profitFactor}")

        assertTrue("Should produce at least 30 trades", result.totalTrades >= 30)
        assertTrue("Should have winning trades", result.winningTrades > 0)
        assertTrue("Should have losing trades", result.losingTrades > 0)
        assertTrue("Win rate should be bounded between 0% and 100%", result.winRatePct in 0.0..100.0)
        assertTrue("Max drawdown should be non-negative", result.maxDrawdownPct >= 0.0)
        assertEquals(result.totalTrades, result.trades.size)

        // Verify that individual trades have both wins and losses
        val wins = result.trades.filter { it.isWin }
        val losses = result.trades.filter { !it.isWin }
        assertFalse(wins.isEmpty())
        assertFalse(losses.isEmpty())

        for (win in wins.take(3)) {
            println("  WIN: #${win.id} ${win.symbol} ${win.side} PnL: +$${win.pnlDollars} (+${win.returnBps} bps) [${win.exitReason}]")
            assertTrue("Win trade PnL must be positive or zero", win.pnlDollars >= 0)
        }
        for (loss in losses.take(3)) {
            println("  LOSS: #${loss.id} ${loss.symbol} ${loss.side} PnL: $${loss.pnlDollars} (${loss.returnBps} bps) [${loss.exitReason}]")
            assertTrue("Loss trade PnL must be negative", winOrLossNegative(loss.pnlDollars))
        }
    }

    @Test
    fun testHighVolShockReplaySimulation() = runBlocking {
        val result = ReplaySimulator.runSimulation("Forex EUR/USD", "High-Vol Shock", 10000.0)

        println("=== REPLAY TEST: HIGH-VOL SHOCK ===")
        println("Trades: ${result.totalTrades}, Wins: ${result.winningTrades}, Losses: ${result.losingTrades}")
        println("Win Rate: ${result.winRatePct}%, Net PnL: $${result.netProfit}, Max DD: ${result.maxDrawdownPct}%")

        assertTrue(result.totalTrades > 20)
        assertTrue(result.winningTrades > 0)
        assertTrue(result.losingTrades > 0)
        assertTrue(result.trades.any { it.exitReason == "STOP_LOSS_CIRCUIT" })
    }

    private fun winOrLossNegative(pnl: Double): Boolean = pnl <= 0.0
}
