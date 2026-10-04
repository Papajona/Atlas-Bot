package com.atlas.trading.ui

import androidx.compose.animation.AnimatedVisibility
import androidx.compose.foundation.Canvas
import androidx.compose.foundation.background
import androidx.compose.foundation.border
import androidx.compose.foundation.clickable
import androidx.compose.foundation.gestures.detectDragGestures
import androidx.compose.foundation.gestures.detectTapGestures
import androidx.compose.foundation.layout.*
import androidx.compose.foundation.lazy.LazyColumn
import androidx.compose.foundation.shape.CircleShape
import androidx.compose.foundation.shape.RoundedCornerShape
import androidx.compose.material.icons.Icons
import androidx.compose.material.icons.filled.*
import androidx.compose.material3.*
import androidx.compose.runtime.*
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.draw.clip
import androidx.compose.ui.draw.shadow
import androidx.compose.ui.geometry.Offset
import androidx.compose.ui.graphics.*
import androidx.compose.ui.graphics.drawscope.Stroke
import androidx.compose.ui.input.pointer.pointerInput
import androidx.compose.ui.platform.testTag
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.unit.dp
import androidx.compose.ui.unit.sp
import java.util.Locale
import kotlin.math.max
import kotlin.math.min

/**
 * Historical daily equity and profit data point for 30-day visualization.
 */
data class EquityPoint(
    val day: Int,
    val dateLabel: String,
    val equity: Double,
    val dailyPnl: Double,
    val cumProfit: Double
)

enum class DashboardTab {
    HOME,
    PORTFOLIO,
    MARKET,
    SETTINGS
}

enum class AssetClass(val label: String, val badge: String) {
    ALL("All Markets", "🌐"),
    CRYPTO("Crypto", "🪙"),
    FOREX("Forex", "💱"),
    COMMODITIES("Commodities", "🥇")
}

@OptIn(ExperimentalMaterial3Api::class)
@Composable
fun UserDashboardScreen(
    userName: String = "Alice",
    backendUrl: String = "",
    onOpenOperatorConsole: () -> Unit = {},
    onOpenPortal: () -> Unit = {}
) {
    var currentTab by remember { mutableStateOf(DashboardTab.HOME) }
    var selectedAssetClass by remember { mutableStateOf(AssetClass.ALL) }
    var selectedTimeframeDays by remember { mutableIntStateOf(30) } // 7 or 30 days
    var actionDialogMessage by remember { mutableStateOf<String?>(null) }

    // Generate 30-day realistic historical equity curve
    val equityHistory = remember {
        generate30DayEquityData()
    }

    val displayPoints = remember(selectedTimeframeDays, equityHistory) {
        if (selectedTimeframeDays == 7) {
            equityHistory.takeLast(7)
        } else {
            equityHistory
        }
    }

    // Dynamically retrieve theme colors supporting Light and Dark modes
    val theme = com.atlas.trading.ui.theme.LocalAtlasColors.current
    val cardSurface = theme.cardSurface
    val cardBorder = theme.cardBorder
    val textPrimary = theme.textPrimary
    val textMuted = theme.textMuted
    val emeraldAccent = theme.emeraldAccent
    val emeraldBadgeBg = theme.emeraldBadgeBg
    val emeraldBadgeText = theme.emeraldBadgeText
    val redDanger = theme.redDanger
    val blueActive = theme.brandAccent
    val brandTeal = theme.brandTeal

    AppAmbientBackground {
        Scaffold(
            modifier = Modifier
                .fillMaxSize()
                .testTag("user_dashboard_scaffold"),
            containerColor = Color.Transparent,
            contentWindowInsets = WindowInsets.safeDrawing,
            bottomBar = {
                DashboardBottomNavigation(
                    currentTab = currentTab,
                    onSelectTab = { tab ->
                        currentTab = tab
                    },
                    blueActive = blueActive,
                    textMuted = textMuted
                )
            }
        ) { innerPadding ->
            when (currentTab) {
                DashboardTab.HOME -> {
                    LazyColumn(
                        modifier = Modifier
                            .fillMaxSize()
                            .padding(innerPadding)
                            .padding(horizontal = 18.dp),
                        verticalArrangement = Arrangement.spacedBy(14.dp)
                    ) {
                        item {
                            Spacer(modifier = Modifier.height(4.dp))
                            DashboardHeader(
                                userName = userName,
                                brandTeal = brandTeal,
                                textPrimary = textPrimary,
                                onAvatarClick = { currentTab = DashboardTab.SETTINGS },
                                onNotificationClick = {
                                    actionDialogMessage = "Active Alerts across Multi-Asset Suite:\n• BTC/USDT: +0.05 bought at $68,450\n• EUR/USD: 4H breakout active (+18 pips)\n• Gold (XAU/USD): 4H trend target hit (+1.15%)"
                                }
                            )
                        }

                        // Asset Class Switcher (Crypto, Forex, Commodities)
                        item {
                            AssetClassSelectorRow(
                                selectedAssetClass = selectedAssetClass,
                                onSelectAsset = { selectedAssetClass = it },
                                textPrimary = textPrimary,
                                textMuted = textMuted,
                                brandTeal = brandTeal
                            )
                        }

                        // Three summary metric cards side by side
                        item {
                            SummaryKpiRow(
                                totalBalance = 1250.00,
                                activeTrades = when (selectedAssetClass) {
                                    AssetClass.ALL -> 5
                                    AssetClass.CRYPTO -> 2
                                    AssetClass.FOREX -> 2
                                    AssetClass.COMMODITIES -> 1
                                },
                                profit24hPct = 3.5,
                                cardSurface = cardSurface,
                                cardBorder = cardBorder,
                                textPrimary = textPrimary,
                                textMuted = textMuted,
                                emeraldBadgeBg = emeraldBadgeBg,
                                emeraldBadgeText = emeraldBadgeText
                            )
                        }

                        // Recharts-style Portfolio Performance (30 Days / 7 Days)
                        item {
                            PortfolioPerformanceCard(
                                points = displayPoints,
                                selectedTimeframe = selectedTimeframeDays,
                                selectedAssetClass = selectedAssetClass,
                                onTimeframeChange = { selectedTimeframeDays = it },
                                cardSurface = cardSurface,
                                cardBorder = cardBorder,
                                textPrimary = textPrimary,
                                textMuted = textMuted,
                                emeraldAccent = emeraldAccent
                            )
                        }

                        // Recent Activity Card (Multi-Asset)
                        item {
                            RecentActivityCard(
                                selectedAssetClass = selectedAssetClass,
                                cardSurface = cardSurface,
                                cardBorder = cardBorder,
                                textPrimary = textPrimary,
                                textMuted = textMuted,
                                emeraldAccent = emeraldAccent,
                                redDanger = redDanger
                            )
                        }

                        // Quick Actions Row (4 buttons)
                        item {
                            QuickActionsRow(
                                cardSurface = cardSurface,
                                cardBorder = cardBorder,
                                textPrimary = textPrimary,
                                onAction = { actionName ->
                                    when (actionName) {
                                        "New Trade" -> actionDialogMessage = "New Paper Trade:\nSelect asset to place disciplined trade:\n• Crypto: BTC/USDT, ETH/USDT\n• Forex: EUR/USD, GBP/USD (OANDA demo)\n• Commodities: Gold XAU/USD (OANDA demo)"
                                        "Deposit" -> actionDialogMessage = "Deposit USDT: Dedicated TRC-20 omnibus deposit addresses enabled."
                                        "Withdraw" -> actionDialogMessage = "Withdrawal Portal: 2-man approval and AAL2 step-up authentication required."
                                        "Analytics" -> currentTab = DashboardTab.PORTFOLIO
                                    }
                                }
                            )
                        }

                        // Watchlist Card (Multi-Asset: Crypto, Forex, Commodities)
                        item {
                            WatchlistCard(
                                selectedAssetClass = selectedAssetClass,
                                cardSurface = cardSurface,
                                cardBorder = cardBorder,
                                textPrimary = textPrimary,
                                textMuted = textMuted,
                                emeraldAccent = emeraldAccent,
                                redDanger = redDanger
                            )
                            Spacer(modifier = Modifier.height(16.dp))
                        }
                    }
                }

                DashboardTab.PORTFOLIO -> {
                    LazyColumn(
                        modifier = Modifier
                            .fillMaxSize()
                            .padding(innerPadding)
                            .padding(horizontal = 18.dp),
                        verticalArrangement = Arrangement.spacedBy(14.dp)
                    ) {
                        item {
                            Spacer(modifier = Modifier.height(4.dp))
                            Row(
                                modifier = Modifier.fillMaxWidth(),
                                horizontalArrangement = Arrangement.SpaceBetween,
                                verticalAlignment = Alignment.CenterVertically
                            ) {
                                Text(
                                    text = "Portfolio Analytics",
                                    fontSize = 22.sp,
                                    fontWeight = FontWeight.Bold,
                                    color = textPrimary
                                )
                                TextButton(onClick = { currentTab = DashboardTab.HOME }) {
                                    Text("Back to Home", color = blueActive)
                                }
                            }
                        }

                        item {
                            AssetClassSelectorRow(
                                selectedAssetClass = selectedAssetClass,
                                onSelectAsset = { selectedAssetClass = it },
                                textPrimary = textPrimary,
                                textMuted = textMuted,
                                brandTeal = brandTeal
                            )
                        }

                        item {
                            PortfolioPerformanceCard(
                                points = displayPoints,
                                selectedTimeframe = selectedTimeframeDays,
                                selectedAssetClass = selectedAssetClass,
                                onTimeframeChange = { selectedTimeframeDays = it },
                                cardSurface = cardSurface,
                                cardBorder = cardBorder,
                                textPrimary = textPrimary,
                                textMuted = textMuted,
                                emeraldAccent = emeraldAccent
                            )
                        }

                        item {
                            StrategyReplaySection()
                            Spacer(modifier = Modifier.height(16.dp))
                        }
                    }
                }

                DashboardTab.MARKET -> {
                    LazyColumn(
                        modifier = Modifier
                            .fillMaxSize()
                            .padding(innerPadding)
                            .padding(horizontal = 18.dp),
                        verticalArrangement = Arrangement.spacedBy(14.dp)
                    ) {
                        item {
                            Spacer(modifier = Modifier.height(4.dp))
                            Row(
                                modifier = Modifier.fillMaxWidth(),
                                horizontalArrangement = Arrangement.SpaceBetween,
                                verticalAlignment = Alignment.CenterVertically
                            ) {
                                Text(
                                    text = "Multi-Asset Scanner",
                                    fontSize = 22.sp,
                                    fontWeight = FontWeight.Bold,
                                    color = textPrimary
                                )
                                TextButton(onClick = { currentTab = DashboardTab.HOME }) {
                                    Text("Back to Home", color = blueActive)
                                }
                            }
                        }

                        item {
                            AssetClassSelectorRow(
                                selectedAssetClass = selectedAssetClass,
                                onSelectAsset = { selectedAssetClass = it },
                                textPrimary = textPrimary,
                                textMuted = textMuted,
                                brandTeal = brandTeal
                            )
                        }

                        item {
                            WatchlistCard(
                                selectedAssetClass = selectedAssetClass,
                                cardSurface = cardSurface,
                                cardBorder = cardBorder,
                                textPrimary = textPrimary,
                                textMuted = textMuted,
                                emeraldAccent = emeraldAccent,
                                redDanger = redDanger
                            )
                        }

                        item {
                            Card(
                                modifier = Modifier.fillMaxWidth(),
                                colors = CardDefaults.cardColors(containerColor = cardSurface),
                                shape = RoundedCornerShape(16.dp),
                                border = CardDefaults.outlinedCardBorder().copy(brush = SolidColor(cardBorder))
                            ) {
                                Column(modifier = Modifier.padding(16.dp)) {
                                    Text(
                                        text = "Automated Scanning Signals",
                                        fontWeight = FontWeight.Bold,
                                        fontSize = 15.sp,
                                        color = textPrimary
                                    )
                                    Spacer(modifier = Modifier.height(8.dp))
                                    Text(
                                        text = "• 🪙 BTC/USDT: 4H Trend Momentum Bullish (ATR = 1,420)\n• 🪙 ETH/USDT: Mean-reversion hold (Z = +0.4)\n• 💱 EUR/USD: London session range breakout passed (1.0852)\n• 💱 GBP/USD: 1D Trend following target active (1.2940)\n• 🥇 Gold (XAU/USD): 4H Channel breakout intact ($2,354.20)",
                                        fontSize = 13.sp,
                                        color = textMuted,
                                        lineHeight = 22.sp
                                    )
                                }
                            }
                            Spacer(modifier = Modifier.height(16.dp))
                        }
                    }
                }

                DashboardTab.SETTINGS -> {
                    ModernSettingsScreen(
                        userName = "$userName Henderson",
                        userEmail = "${userName.lowercase(Locale.US)}.henderson@atlas.trading",
                        onOpenOperatorConsole = onOpenOperatorConsole,
                        onOpenPortal = onOpenPortal,
                        modifier = Modifier.padding(innerPadding)
                    )
                }
            }
        }
    }

    if (actionDialogMessage != null) {
        AlertDialog(
            onDismissRequest = { actionDialogMessage = null },
            title = {
                Text("Atlas Trading System", fontWeight = FontWeight.Bold)
            },
            text = {
                Text(actionDialogMessage ?: "")
            },
            confirmButton = {
                TextButton(onClick = { actionDialogMessage = null }) {
                    Text("OK", color = brandTeal, fontWeight = FontWeight.Bold)
                }
            },
            containerColor = cardSurface
        )
    }
}

@Composable
private fun DashboardHeader(
    userName: String,
    brandTeal: Color,
    textPrimary: Color,
    onAvatarClick: () -> Unit,
    onNotificationClick: () -> Unit
) {
    Column {
        Row(
            modifier = Modifier.fillMaxWidth(),
            horizontalArrangement = Arrangement.SpaceBetween,
            verticalAlignment = Alignment.CenterVertically
        ) {
            // Brand Logo "A"
            Box(
                modifier = Modifier
                    .size(36.dp)
                    .clip(RoundedCornerShape(10.dp))
                    .background(Color(0xFFE6F4F1)),
                contentAlignment = Alignment.Center
            ) {
                Text(
                    text = "A",
                    fontSize = 20.sp,
                    fontWeight = FontWeight.Black,
                    color = brandTeal
                )
            }

            // Top Right Actions: Avatar & Notification Bell
            Row(
                horizontalArrangement = Arrangement.spacedBy(10.dp),
                verticalAlignment = Alignment.CenterVertically
            ) {
                IconButton(
                    onClick = onAvatarClick,
                    modifier = Modifier
                        .size(36.dp)
                        .clip(CircleShape)
                        .background(Color(0xFFE2E8F0))
                ) {
                    Icon(
                        imageVector = Icons.Default.Person,
                        contentDescription = "User Profile",
                        tint = Color(0xFF475569),
                        modifier = Modifier.size(20.dp)
                    )
                }

                Box(
                    modifier = Modifier
                        .size(36.dp)
                        .clip(CircleShape)
                        .background(Color(0xFFF1F5F9))
                        .clickable(onClick = onNotificationClick),
                    contentAlignment = Alignment.Center
                ) {
                    Icon(
                        imageVector = Icons.Default.Notifications,
                        contentDescription = "Notifications",
                        tint = Color(0xFF334155),
                        modifier = Modifier.size(20.dp)
                    )
                    // Red unread notification dot
                    Box(
                        modifier = Modifier
                            .align(Alignment.TopEnd)
                            .padding(top = 7.dp, end = 7.dp)
                            .size(7.dp)
                            .clip(CircleShape)
                            .background(Color(0xFFEF4444))
                    )
                }
            }
        }

        Spacer(modifier = Modifier.height(14.dp))

        Text(
            text = "Welcome, $userName",
            fontSize = 22.sp,
            fontWeight = FontWeight.Bold,
            color = textPrimary
        )
    }
}

@Composable
private fun AssetClassSelectorRow(
    selectedAssetClass: AssetClass,
    onSelectAsset: (AssetClass) -> Unit,
    textPrimary: Color,
    textMuted: Color,
    brandTeal: Color
) {
    val theme = com.atlas.trading.ui.theme.LocalAtlasColors.current
    Row(
        modifier = Modifier.fillMaxWidth(),
        horizontalArrangement = Arrangement.spacedBy(6.dp)
    ) {
        listOf(AssetClass.ALL, AssetClass.CRYPTO, AssetClass.FOREX, AssetClass.COMMODITIES).forEach { asset ->
            val isSelected = selectedAssetClass == asset
            Box(
                modifier = Modifier
                    .weight(1f)
                    .clip(RoundedCornerShape(10.dp))
                    .background(if (isSelected) theme.emeraldAccent else theme.cardSurface)
                    .border(
                        width = 1.dp,
                        color = if (isSelected) theme.emeraldAccent else theme.cardBorder,
                        shape = RoundedCornerShape(10.dp)
                    )
                    .clickable { onSelectAsset(asset) }
                    .padding(vertical = 7.dp),
                contentAlignment = Alignment.Center
            ) {
                Text(
                    text = "${asset.badge} ${asset.label.split(" ").first()}",
                    fontSize = 11.sp,
                    fontWeight = if (isSelected) FontWeight.Bold else FontWeight.Medium,
                    color = if (isSelected) Color.White else textPrimary,
                    maxLines = 1
                )
            }
        }
    }
}

@Composable
private fun SummaryKpiRow(
    totalBalance: Double,
    activeTrades: Int,
    profit24hPct: Double,
    cardSurface: Color,
    cardBorder: Color,
    textPrimary: Color,
    textMuted: Color,
    emeraldBadgeBg: Color,
    emeraldBadgeText: Color
) {
    Row(
        modifier = Modifier.fillMaxWidth(),
        horizontalArrangement = Arrangement.spacedBy(10.dp)
    ) {
        // Total Balance Card
        Card(
            modifier = Modifier.weight(1.1f),
            colors = CardDefaults.cardColors(containerColor = cardSurface),
            shape = RoundedCornerShape(16.dp),
            border = CardDefaults.outlinedCardBorder().copy(brush = SolidColor(cardBorder))
        ) {
            Column(modifier = Modifier.padding(horizontal = 12.dp, vertical = 10.dp)) {
                Text(text = "Total Balance", fontSize = 11.sp, color = textMuted, maxLines = 1)
                Spacer(modifier = Modifier.height(3.dp))
                Text(
                    text = "$${String.format(Locale.US, "%,.2f", totalBalance)}",
                    fontSize = 15.sp,
                    fontWeight = FontWeight.Bold,
                    color = textPrimary,
                    maxLines = 1
                )
            }
        }

        // Active Trades Card
        Card(
            modifier = Modifier.weight(0.9f),
            colors = CardDefaults.cardColors(containerColor = cardSurface),
            shape = RoundedCornerShape(16.dp),
            border = CardDefaults.outlinedCardBorder().copy(brush = SolidColor(cardBorder))
        ) {
            Column(modifier = Modifier.padding(horizontal = 12.dp, vertical = 10.dp)) {
                Text(text = "Active Trades", fontSize = 11.sp, color = textMuted, maxLines = 1)
                Spacer(modifier = Modifier.height(3.dp))
                Text(
                    text = "$activeTrades",
                    fontSize = 15.sp,
                    fontWeight = FontWeight.Bold,
                    color = textPrimary
                )
            }
        }

        // Profit (24h) Card
        Card(
            modifier = Modifier.weight(1.0f),
            colors = CardDefaults.cardColors(containerColor = cardSurface),
            shape = RoundedCornerShape(16.dp),
            border = CardDefaults.outlinedCardBorder().copy(brush = SolidColor(cardBorder))
        ) {
            Column(modifier = Modifier.padding(horizontal = 12.dp, vertical = 10.dp)) {
                Text(text = "Profit (24h)", fontSize = 11.sp, color = textMuted, maxLines = 1)
                Spacer(modifier = Modifier.height(3.dp))
                Box(
                    modifier = Modifier
                        .clip(RoundedCornerShape(6.dp))
                        .background(emeraldBadgeBg)
                        .padding(horizontal = 6.dp, vertical = 2.dp)
                ) {
                    Text(
                        text = "+${String.format(Locale.US, "%.1f", profit24hPct)}%",
                        fontSize = 13.sp,
                        fontWeight = FontWeight.Bold,
                        color = emeraldBadgeText
                    )
                }
            }
        }
    }
}

/**
 * Recharts-style Interactive Area Chart Component.
 * Implements smooth cubic Bézier spline interpolation and transparent gradient fill.
 */
@Composable
private fun PortfolioPerformanceCard(
    points: List<EquityPoint>,
    selectedTimeframe: Int,
    selectedAssetClass: AssetClass = AssetClass.ALL,
    onTimeframeChange: (Int) -> Unit,
    cardSurface: Color,
    cardBorder: Color,
    textPrimary: Color,
    textMuted: Color,
    emeraldAccent: Color
) {
    var selectedIndex by remember { mutableStateOf<Int?>(null) }

    Card(
        modifier = Modifier
            .fillMaxWidth()
            .testTag("portfolio_performance_card"),
        colors = CardDefaults.cardColors(containerColor = cardSurface),
        shape = RoundedCornerShape(20.dp),
        border = CardDefaults.outlinedCardBorder().copy(brush = SolidColor(cardBorder))
    ) {
        Column(modifier = Modifier.padding(16.dp)) {
            // Title and Timeframe Toggle
            Row(
                modifier = Modifier.fillMaxWidth(),
                horizontalArrangement = Arrangement.SpaceBetween,
                verticalAlignment = Alignment.CenterVertically
            ) {
                val assetSuffix = if (selectedAssetClass != AssetClass.ALL) " • ${selectedAssetClass.label}" else ""
                Text(
                    text = "Portfolio Performance (${selectedTimeframe}D$assetSuffix)",
                    fontSize = 13.sp,
                    fontWeight = FontWeight.SemiBold,
                    color = textPrimary
                )

                val theme = com.atlas.trading.ui.theme.LocalAtlasColors.current
                Row(
                    modifier = Modifier
                        .clip(RoundedCornerShape(8.dp))
                        .background(theme.cardInnerSurface)
                        .padding(2.dp),
                    horizontalArrangement = Arrangement.spacedBy(2.dp)
                ) {
                    listOf(7, 30).forEach { tf ->
                        val isSelected = selectedTimeframe == tf
                        Box(
                            modifier = Modifier
                                .clip(RoundedCornerShape(6.dp))
                                .background(if (isSelected) theme.cardSurface else Color.Transparent)
                                .clickable {
                                    selectedIndex = null
                                    onTimeframeChange(tf)
                                }
                                .padding(horizontal = 8.dp, vertical = 3.dp)
                        ) {
                            Text(
                                text = "${tf}D",
                                fontSize = 10.sp,
                                fontWeight = if (isSelected) FontWeight.Bold else FontWeight.Normal,
                                color = if (isSelected) textPrimary else textMuted
                            )
                        }
                    }
                }
            }

            Spacer(modifier = Modifier.height(10.dp))

            // Tooltip or Active Value Display
            val activePoint = selectedIndex?.let { points.getOrNull(it) } ?: points.lastOrNull()
            if (activePoint != null) {
                Row(
                    modifier = Modifier.fillMaxWidth(),
                    horizontalArrangement = Arrangement.SpaceBetween,
                    verticalAlignment = Alignment.Bottom
                ) {
                    Column {
                        Text(
                            text = "$${String.format(Locale.US, "%,.2f", activePoint.equity)}",
                            fontSize = 20.sp,
                            fontWeight = FontWeight.Bold,
                            color = textPrimary
                        )
                        Text(
                            text = "${activePoint.dateLabel} • Cum. PnL: +$${String.format(Locale.US, "%.2f", activePoint.cumProfit)}",
                            fontSize = 11.sp,
                            color = emeraldAccent
                        )
                    }
                    Text(
                        text = if (selectedIndex != null) "Scrubbing active" else "Real-time curve",
                        fontSize = 10.sp,
                        color = textMuted
                    )
                }
            }

            Spacer(modifier = Modifier.height(12.dp))

            // The Recharts-style Area Curve
            RechartsMonotoneAreaCanvas(
                points = points,
                selectedIndex = selectedIndex,
                onSelectIndex = { selectedIndex = it },
                accentColor = emeraldAccent,
                modifier = Modifier
                    .fillMaxWidth()
                    .height(140.dp)
            )
        }
    }
}

@Composable
private fun RechartsMonotoneAreaCanvas(
    points: List<EquityPoint>,
    selectedIndex: Int?,
    onSelectIndex: (Int?) -> Unit,
    accentColor: Color,
    modifier: Modifier = Modifier
) {
    if (points.isEmpty()) return

    val minEquity = points.minOf { it.equity } * 0.985
    val maxEquity = points.maxOf { it.equity } * 1.015
    val equityRange = max(1.0, maxEquity - minEquity)

    Canvas(
        modifier = modifier
            .pointerInput(points) {
                detectTapGestures(
                    onPress = { offset ->
                        val stepX = size.width / (points.size - 1).coerceAtLeast(1)
                        val idx = (offset.x / stepX).toInt().coerceIn(0, points.size - 1)
                        onSelectIndex(idx)
                    }
                )
            }
            .pointerInput(points) {
                detectDragGestures(
                    onDragStart = { offset ->
                        val stepX = size.width / (points.size - 1).coerceAtLeast(1)
                        val idx = (offset.x / stepX).toInt().coerceIn(0, points.size - 1)
                        onSelectIndex(idx)
                    },
                    onDragEnd = { onSelectIndex(null) },
                    onDragCancel = { onSelectIndex(null) },
                    onDrag = { change, _ ->
                        change.consume()
                        val stepX = size.width / (points.size - 1).coerceAtLeast(1)
                        val idx = (change.position.x / stepX).toInt().coerceIn(0, points.size - 1)
                        onSelectIndex(idx)
                    }
                )
            }
    ) {
        val width = size.width
        val height = size.height
        val n = points.size

        if (n < 2) return@Canvas

        val stepX = width / (n - 1)
        val computedCoords = points.mapIndexed { idx, point ->
            val x = idx * stepX
            val normalizedY = (point.equity - minEquity) / equityRange
            val y = (height * (1f - normalizedY.toFloat())).coerceIn(10f, height - 10f)
            Offset(x, y)
        }

        // Build smooth cubic Bézier path (matching Recharts type="monotone")
        val strokePath = Path().apply {
            moveTo(computedCoords.first().x, computedCoords.first().y)
            for (i in 0 until computedCoords.size - 1) {
                val p0 = computedCoords[max(0, i - 1)]
                val p1 = computedCoords[i]
                val p2 = computedCoords[i + 1]
                val p3 = computedCoords[min(computedCoords.size - 1, i + 2)]

                val cp1x = p1.x + (p2.x - p0.x) / 6f
                val cp1y = p1.y + (p2.y - p0.y) / 6f
                val cp2x = p2.x - (p3.x - p1.x) / 6f
                val cp2y = p2.y - (p3.y - p1.y) / 6f

                cubicTo(cp1x, cp1y, cp2x, cp2y, p2.x, p2.y)
            }
        }

        // Closed area path for soft gradient fill
        val fillPath = Path().apply {
            addPath(strokePath)
            lineTo(width, height)
            lineTo(0f, height)
            close()
        }

        // 1. Draw gradient area fill
        drawPath(
            path = fillPath,
            brush = Brush.verticalGradient(
                colors = listOf(
                    accentColor.copy(alpha = 0.35f),
                    accentColor.copy(alpha = 0.08f),
                    accentColor.copy(alpha = 0.00f)
                ),
                startY = 0f,
                endY = height
            )
        )

        // 2. Draw smooth stroke curve
        drawPath(
            path = strokePath,
            color = accentColor,
            style = Stroke(
                width = 3.5.dp.toPx(),
                cap = StrokeCap.Round,
                join = StrokeJoin.Round
            )
        )

        // 3. Draw active point or scrub indicator
        val activeIdx = selectedIndex ?: (points.size - 1)
        val activeCoord = computedCoords[activeIdx]

        // Guide vertical line if scrubbing
        if (selectedIndex != null) {
            drawLine(
                color = accentColor.copy(alpha = 0.4f),
                start = Offset(activeCoord.x, 0f),
                end = Offset(activeCoord.x, height),
                strokeWidth = 1.dp.toPx(),
                pathEffect = PathEffect.dashPathEffect(floatArrayOf(10f, 10f))
            )
        }

        // Active glowing circle
        drawCircle(
            color = accentColor.copy(alpha = 0.25f),
            radius = 7.dp.toPx(),
            center = activeCoord
        )
        drawCircle(
            color = Color.White,
            radius = 4.dp.toPx(),
            center = activeCoord
        )
        drawCircle(
            color = accentColor,
            radius = 3.dp.toPx(),
            center = activeCoord
        )
    }
}

@Composable
private fun RecentActivityCard(
    selectedAssetClass: AssetClass = AssetClass.ALL,
    cardSurface: Color,
    cardBorder: Color,
    textPrimary: Color,
    textMuted: Color,
    emeraldAccent: Color,
    redDanger: Color
) {
    val theme = com.atlas.trading.ui.theme.LocalAtlasColors.current
    Card(
        modifier = Modifier.fillMaxWidth(),
        colors = CardDefaults.cardColors(containerColor = cardSurface),
        shape = RoundedCornerShape(18.dp),
        border = CardDefaults.outlinedCardBorder().copy(brush = SolidColor(cardBorder))
    ) {
        Column(modifier = Modifier.padding(16.dp)) {
            Row(
                modifier = Modifier.fillMaxWidth(),
                horizontalArrangement = Arrangement.SpaceBetween,
                verticalAlignment = Alignment.CenterVertically
            ) {
                Text(
                    text = "Recent Activity",
                    fontSize = 14.sp,
                    fontWeight = FontWeight.SemiBold,
                    color = textPrimary
                )
                Text(
                    text = selectedAssetClass.label,
                    fontSize = 11.sp,
                    fontWeight = FontWeight.Medium,
                    color = textMuted
                )
            }

            Spacer(modifier = Modifier.height(12.dp))

            when (selectedAssetClass) {
                AssetClass.FOREX -> {
                    // EUR/USD activity
                    ActivityRow(
                        badgeText = "€",
                        badgeBg = Color(0xFF3B82F6),
                        title = "EUR/USD bought:",
                        value = "+25 pips",
                        valueColor = emeraldAccent,
                        textPrimary = textPrimary
                    )
                    Spacer(modifier = Modifier.height(10.dp))
                    HorizontalDivider(color = theme.divider, thickness = 1.dp)
                    Spacer(modifier = Modifier.height(10.dp))
                    // GBP/USD activity
                    ActivityRow(
                        badgeText = "£",
                        badgeBg = Color(0xFF8B5CF6),
                        title = "GBP/USD sold:",
                        value = "+40 pips",
                        valueColor = emeraldAccent,
                        textPrimary = textPrimary
                    )
                }
                AssetClass.COMMODITIES -> {
                    // Gold activity
                    ActivityRow(
                        badgeText = "Au",
                        badgeBg = Color(0xFFEAB308),
                        title = "Gold (XAU) bought:",
                        value = "+0.4 oz (TP)",
                        valueColor = emeraldAccent,
                        textPrimary = textPrimary
                    )
                    Spacer(modifier = Modifier.height(10.dp))
                    HorizontalDivider(color = theme.divider, thickness = 1.dp)
                    Spacer(modifier = Modifier.height(10.dp))
                    // Crude Oil activity
                    ActivityRow(
                        badgeText = "🛢",
                        badgeBg = Color(0xFF475569),
                        title = "Crude Oil (WTI) sold:",
                        value = "+$1.20/bbl",
                        valueColor = emeraldAccent,
                        textPrimary = textPrimary
                    )
                }
                else -> {
                    // Crypto & Multi-Asset default
                    ActivityRow(
                        badgeText = "₿",
                        badgeBg = Color(0xFFF7931A),
                        title = "BTC bought:",
                        value = "+0.05",
                        valueColor = emeraldAccent,
                        textPrimary = textPrimary
                    )
                    Spacer(modifier = Modifier.height(10.dp))
                    HorizontalDivider(color = theme.divider, thickness = 1.dp)
                    Spacer(modifier = Modifier.height(10.dp))
                    ActivityRow(
                        badgeText = "Ξ",
                        badgeBg = Color(0xFF627EEA),
                        title = "ETH sold:",
                        value = "-0.1",
                        valueColor = redDanger,
                        textPrimary = textPrimary
                    )
                    Spacer(modifier = Modifier.height(10.dp))
                    HorizontalDivider(color = theme.divider, thickness = 1.dp)
                    Spacer(modifier = Modifier.height(10.dp))
                    ActivityRow(
                        badgeText = "€",
                        badgeBg = Color(0xFF3B82F6),
                        title = "EUR/USD limit filled:",
                        value = "+18 pips",
                        valueColor = emeraldAccent,
                        textPrimary = textPrimary
                    )
                }
            }
        }
    }
}

@Composable
private fun ActivityRow(
    badgeText: String,
    badgeBg: Color,
    title: String,
    value: String,
    valueColor: Color,
    textPrimary: Color
) {
    Row(
        modifier = Modifier.fillMaxWidth(),
        horizontalArrangement = Arrangement.SpaceBetween,
        verticalAlignment = Alignment.CenterVertically
    ) {
        Row(verticalAlignment = Alignment.CenterVertically) {
            Box(
                modifier = Modifier
                    .size(28.dp)
                    .clip(CircleShape)
                    .background(badgeBg),
                contentAlignment = Alignment.Center
            ) {
                Text(badgeText, color = Color.White, fontWeight = FontWeight.Bold, fontSize = 13.sp)
            }
            Spacer(modifier = Modifier.width(10.dp))
            Text(
                text = title,
                fontSize = 13.sp,
                color = textPrimary,
                fontWeight = FontWeight.Normal
            )
        }

        Text(
            text = value,
            fontSize = 13.sp,
            fontWeight = FontWeight.Bold,
            color = valueColor
        )
    }
}

@Composable
private fun QuickActionsRow(
    cardSurface: Color,
    cardBorder: Color,
    textPrimary: Color,
    onAction: (String) -> Unit
) {
    Row(
        modifier = Modifier.fillMaxWidth(),
        horizontalArrangement = Arrangement.spacedBy(8.dp)
    ) {
        val actions = listOf(
            Triple("New Trade", Icons.Default.Refresh, "trade"),
            Triple("Deposit", Icons.Default.ArrowDropDown, "deposit"),
            Triple("Withdraw", Icons.Default.KeyboardArrowUp, "withdraw"),
            Triple("Analytics", Icons.Default.Info, "analytics")
        )

        actions.forEach { (label, icon, key) ->
            Card(
                modifier = Modifier
                    .weight(1f)
                    .clickable { onAction(label) },
                colors = CardDefaults.cardColors(containerColor = cardSurface),
                shape = RoundedCornerShape(14.dp),
                border = CardDefaults.outlinedCardBorder().copy(brush = SolidColor(cardBorder))
            ) {
                Column(
                    modifier = Modifier
                        .fillMaxWidth()
                        .padding(vertical = 10.dp, horizontal = 4.dp),
                    horizontalAlignment = Alignment.CenterHorizontally
                ) {
                    Icon(
                        imageVector = icon,
                        contentDescription = label,
                        tint = textPrimary,
                        modifier = Modifier.size(20.dp)
                    )
                    Spacer(modifier = Modifier.height(4.dp))
                    Text(
                        text = label,
                        fontSize = 10.sp,
                        fontWeight = FontWeight.Medium,
                        color = textPrimary,
                        maxLines = 1
                    )
                }
            }
        }
    }
}

@Composable
private fun WatchlistCard(
    selectedAssetClass: AssetClass = AssetClass.ALL,
    cardSurface: Color,
    cardBorder: Color,
    textPrimary: Color,
    textMuted: Color,
    emeraldAccent: Color,
    redDanger: Color
) {
    Card(
        modifier = Modifier.fillMaxWidth(),
        colors = CardDefaults.cardColors(containerColor = cardSurface),
        shape = RoundedCornerShape(18.dp),
        border = CardDefaults.outlinedCardBorder().copy(brush = SolidColor(cardBorder))
    ) {
        Column(modifier = Modifier.padding(16.dp)) {
            Row(
                modifier = Modifier.fillMaxWidth(),
                horizontalArrangement = Arrangement.SpaceBetween,
                verticalAlignment = Alignment.CenterVertically
            ) {
                Text(
                    text = "Watchlist (${selectedAssetClass.label})",
                    fontSize = 14.sp,
                    fontWeight = FontWeight.SemiBold,
                    color = textPrimary
                )
                Text(
                    text = "Live Scanner",
                    fontSize = 11.sp,
                    color = emeraldAccent,
                    fontWeight = FontWeight.Bold
                )
            }

            Spacer(modifier = Modifier.height(10.dp))

            when (selectedAssetClass) {
                AssetClass.FOREX -> {
                    // EUR/USD
                    WatchlistItemRow(
                        badge = "€",
                        badgeBg = Color(0xFF3B82F6),
                        symbol = "EUR/USD",
                        price = "1.0852",
                        changePct = "+0.35%",
                        isBullish = true,
                        color = emeraldAccent,
                        textPrimary = textPrimary,
                        textMuted = textMuted
                    )
                    Spacer(modifier = Modifier.height(8.dp))
                    HorizontalDivider(color = Color(0xFFF1F5F9), thickness = 1.dp)
                    Spacer(modifier = Modifier.height(8.dp))
                    // GBP/USD
                    WatchlistItemRow(
                        badge = "£",
                        badgeBg = Color(0xFF8B5CF6),
                        symbol = "GBP/USD",
                        price = "1.2940",
                        changePct = "-0.22%",
                        isBullish = false,
                        color = redDanger,
                        textPrimary = textPrimary,
                        textMuted = textMuted
                    )
                    Spacer(modifier = Modifier.height(8.dp))
                    HorizontalDivider(color = Color(0xFFF1F5F9), thickness = 1.dp)
                    Spacer(modifier = Modifier.height(8.dp))
                    // USD/JPY
                    WatchlistItemRow(
                        badge = "¥",
                        badgeBg = Color(0xFFEC4899),
                        symbol = "USD/JPY",
                        price = "154.20",
                        changePct = "+0.48%",
                        isBullish = true,
                        color = emeraldAccent,
                        textPrimary = textPrimary,
                        textMuted = textMuted
                    )
                }
                AssetClass.COMMODITIES -> {
                    // Gold
                    WatchlistItemRow(
                        badge = "Au",
                        badgeBg = Color(0xFFEAB308),
                        symbol = "Gold (XAU)",
                        price = "$2,354.20",
                        changePct = "+1.15%",
                        isBullish = true,
                        color = emeraldAccent,
                        textPrimary = textPrimary,
                        textMuted = textMuted
                    )
                    Spacer(modifier = Modifier.height(8.dp))
                    HorizontalDivider(color = Color(0xFFF1F5F9), thickness = 1.dp)
                    Spacer(modifier = Modifier.height(8.dp))
                    // Silver
                    WatchlistItemRow(
                        badge = "Ag",
                        badgeBg = Color(0xFF94A3B8),
                        symbol = "Silver (XAG)",
                        price = "$28.45",
                        changePct = "+0.82%",
                        isBullish = true,
                        color = emeraldAccent,
                        textPrimary = textPrimary,
                        textMuted = textMuted
                    )
                    Spacer(modifier = Modifier.height(8.dp))
                    HorizontalDivider(color = Color(0xFFF1F5F9), thickness = 1.dp)
                    Spacer(modifier = Modifier.height(8.dp))
                    // Crude Oil
                    WatchlistItemRow(
                        badge = "🛢",
                        badgeBg = Color(0xFF475569),
                        symbol = "Crude (WTI)",
                        price = "$78.60",
                        changePct = "-0.90%",
                        isBullish = false,
                        color = redDanger,
                        textPrimary = textPrimary,
                        textMuted = textMuted
                    )
                }
                else -> {
                    // Crypto or ALL
                    WatchlistItemRow(
                        badge = "₿",
                        badgeBg = Color(0xFFF7931A),
                        symbol = "BTC",
                        price = "$68,450",
                        changePct = "+2.4%",
                        isBullish = true,
                        color = emeraldAccent,
                        textPrimary = textPrimary,
                        textMuted = textMuted
                    )
                    Spacer(modifier = Modifier.height(8.dp))
                    HorizontalDivider(color = Color(0xFFF1F5F9), thickness = 1.dp)
                    Spacer(modifier = Modifier.height(8.dp))
                    WatchlistItemRow(
                        badge = "Ξ",
                        badgeBg = Color(0xFF627EEA),
                        symbol = "ETH",
                        price = "$3,480",
                        changePct = "-1.1%",
                        isBullish = false,
                        color = redDanger,
                        textPrimary = textPrimary,
                        textMuted = textMuted
                    )
                    if (selectedAssetClass == AssetClass.ALL) {
                        Spacer(modifier = Modifier.height(8.dp))
                        HorizontalDivider(color = Color(0xFFF1F5F9), thickness = 1.dp)
                        Spacer(modifier = Modifier.height(8.dp))
                        WatchlistItemRow(
                            badge = "€",
                            badgeBg = Color(0xFF3B82F6),
                            symbol = "EUR/USD",
                            price = "1.0852",
                            changePct = "+0.35%",
                            isBullish = true,
                            color = emeraldAccent,
                            textPrimary = textPrimary,
                            textMuted = textMuted
                        )
                        Spacer(modifier = Modifier.height(8.dp))
                        HorizontalDivider(color = Color(0xFFF1F5F9), thickness = 1.dp)
                        Spacer(modifier = Modifier.height(8.dp))
                        WatchlistItemRow(
                            badge = "Au",
                            badgeBg = Color(0xFFEAB308),
                            symbol = "Gold (XAU)",
                            price = "$2,354.20",
                            changePct = "+1.15%",
                            isBullish = true,
                            color = emeraldAccent,
                            textPrimary = textPrimary,
                            textMuted = textMuted
                        )
                    }
                }
            }
        }
    }
}

@Composable
private fun WatchlistItemRow(
    badge: String,
    badgeBg: Color,
    symbol: String,
    price: String,
    changePct: String,
    isBullish: Boolean,
    color: Color,
    textPrimary: Color,
    textMuted: Color
) {
    Row(
        modifier = Modifier.fillMaxWidth(),
        horizontalArrangement = Arrangement.SpaceBetween,
        verticalAlignment = Alignment.CenterVertically
    ) {
        Row(verticalAlignment = Alignment.CenterVertically) {
            Box(
                modifier = Modifier
                    .size(24.dp)
                    .clip(CircleShape)
                    .background(badgeBg),
                contentAlignment = Alignment.Center
            ) {
                Text(badge, color = Color.White, fontWeight = FontWeight.Bold, fontSize = 11.sp)
            }
            Spacer(modifier = Modifier.width(8.dp))
            Text(text = symbol, fontSize = 13.sp, fontWeight = FontWeight.Bold, color = textPrimary)
            Spacer(modifier = Modifier.width(6.dp))
            Text(text = price, fontSize = 12.sp, color = textMuted)
        }

        Row(verticalAlignment = Alignment.CenterVertically) {
            MiniSparklineCanvas(isBullish = isBullish, color = color)
            Spacer(modifier = Modifier.width(8.dp))
            Text(text = changePct, fontSize = 12.sp, fontWeight = FontWeight.Bold, color = color)
        }
    }
}

@Composable
private fun MiniSparklineCanvas(
    isBullish: Boolean,
    color: Color,
    modifier: Modifier = Modifier
) {
    Canvas(modifier = modifier.size(width = 44.dp, height = 18.dp)) {
        val w = size.width
        val h = size.height
        val path = Path().apply {
            if (isBullish) {
                moveTo(0f, h * 0.8f)
                cubicTo(w * 0.25f, h * 0.7f, w * 0.5f, h * 0.4f, w * 0.75f, h * 0.5f)
                lineTo(w, h * 0.1f)
            } else {
                moveTo(0f, h * 0.2f)
                cubicTo(w * 0.25f, h * 0.3f, w * 0.5f, h * 0.6f, w * 0.75f, h * 0.4f)
                lineTo(w, h * 0.9f)
            }
        }
        drawPath(
            path = path,
            color = color,
            style = Stroke(width = 2.dp.toPx(), cap = StrokeCap.Round)
        )
    }
}

@Composable
private fun DashboardBottomNavigation(
    currentTab: DashboardTab,
    onSelectTab: (DashboardTab) -> Unit,
    blueActive: Color,
    textMuted: Color
) {
    val theme = com.atlas.trading.ui.theme.LocalAtlasColors.current
    Surface(
        modifier = Modifier
            .fillMaxWidth()
            .navigationBarsPadding(),
        color = theme.bottomNavSurface,
        shadowElevation = 10.dp
    ) {
        Row(
            modifier = Modifier
                .fillMaxWidth()
                .padding(vertical = 8.dp),
            horizontalArrangement = Arrangement.SpaceAround,
            verticalAlignment = Alignment.CenterVertically
        ) {
            val tabs = listOf(
                Pair(DashboardTab.HOME, Pair("Home", Icons.Default.Home)),
                Pair(DashboardTab.PORTFOLIO, Pair("Portfolio", Icons.Default.Info)),
                Pair(DashboardTab.MARKET, Pair("Market", Icons.Default.PlayArrow)),
                Pair(DashboardTab.SETTINGS, Pair("Settings", Icons.Default.Settings))
            )

            tabs.forEach { (tab, meta) ->
                val (label, icon) = meta
                val isActive = currentTab == tab
                Column(
                    modifier = Modifier
                        .clickable { onSelectTab(tab) }
                        .padding(horizontal = 12.dp, vertical = 4.dp),
                    horizontalAlignment = Alignment.CenterHorizontally
                ) {
                    Icon(
                        imageVector = icon,
                        contentDescription = label,
                        tint = if (isActive) blueActive else textMuted,
                        modifier = Modifier.size(22.dp)
                    )
                    Spacer(modifier = Modifier.height(2.dp))
                    Text(
                        text = label,
                        fontSize = 10.sp,
                        fontWeight = if (isActive) FontWeight.Bold else FontWeight.Normal,
                        color = if (isActive) blueActive else textMuted
                    )
                }
            }
        }
    }
}

/**
 * Generates realistic 30-day daily historical equity progression with positive trend.
 */
private fun generate30DayEquityData(): List<EquityPoint> {
    val result = mutableListOf<EquityPoint>()
    var equity = 1064.60
    var cumProfit = 0.0

    // Modeled realistic 30-day progression ending at $1,250.00
    val dailyChanges = listOf(
        +4.20, +8.50, -3.10, +6.40, +12.30, -5.20, +9.10,
        +14.50, -4.80, +11.20, +7.60, -6.10, +18.40, +10.50,
        -7.20, +15.30, +8.90, -4.50, +13.20, +16.80, -8.10,
        +12.40, +9.50, -3.80, +14.60, +17.20, -5.40, +12.80,
        +10.60, +9.50
    )

    for (day in 1..30) {
        val change = dailyChanges[day - 1]
        equity += change
        cumProfit += change
        val dateLabel = "Day $day"
        result.add(
            EquityPoint(
                day = day,
                dateLabel = dateLabel,
                equity = Math.round(equity * 100.0) / 100.0,
                dailyPnl = change,
                cumProfit = Math.round(cumProfit * 100.0) / 100.0
            )
        )
    }
    return result
}
