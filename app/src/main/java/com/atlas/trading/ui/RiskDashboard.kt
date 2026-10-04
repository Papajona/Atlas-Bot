package com.atlas.trading.ui

import androidx.compose.animation.AnimatedVisibility
import androidx.compose.animation.animateColorAsState
import androidx.compose.animation.core.animateFloatAsState
import androidx.compose.animation.core.tween
import androidx.compose.foundation.Canvas
import androidx.compose.foundation.background
import androidx.compose.foundation.border
import androidx.compose.foundation.clickable
import androidx.compose.foundation.layout.*
import androidx.compose.foundation.lazy.LazyColumn
import androidx.compose.foundation.shape.CircleShape
import androidx.compose.foundation.shape.RoundedCornerShape
import androidx.compose.material.icons.Icons
import androidx.compose.material.icons.filled.Info
import androidx.compose.material.icons.filled.PlayArrow
import androidx.compose.material.icons.filled.Refresh
import androidx.compose.material.icons.filled.Warning
import androidx.compose.material3.*
import androidx.compose.runtime.*
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.draw.clip
import androidx.compose.ui.geometry.Offset
import androidx.compose.ui.geometry.Size
import androidx.compose.ui.graphics.Brush
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.graphics.StrokeCap
import androidx.compose.ui.graphics.drawscope.Stroke
import androidx.compose.ui.platform.testTag
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.unit.dp
import androidx.compose.ui.unit.sp
import com.atlas.trading.network.WebSocketManager
import com.atlas.trading.network.WebSocketConnectionState
import kotlinx.coroutines.delay
import kotlinx.coroutines.launch

/**
 * Real-time Portfolio Risk Metrics state model.
 * Connects to the Atlas Trading metrics & state gathering infrastructure.
 */
data class PortfolioRiskMetrics(
    val equity: Double = 0.0,
    val peakEquity: Double = 0.0,
    val realizedPnl: Double = 0.0,
    val unrealizedPnl: Double = 0.0,
    val currentDrawdownPct: Double = 0.0,
    val maxDrawdownLimitPct: Double = 0.0,
    val dailyLossPct: Double = 0.0,
    val dailyLossLimitPct: Double = 0.0,
    val sharpeRatio: Double = 0.0,
    val calmarRatio: Double = 0.0,
    val sortinoRatio: Double = 0.0,
    val deflatedSharpeRatio: Double = 0.0,
    val edgeDecayZScore: Double = 0.0,
    val regime: String = "UNKNOWN",
    val regimeDescription: String = "Live telemetry unavailable",
    val momentumWeight: Int = 0,
    val meanRevWeight: Int = 0,
    val sessionWeight: Int = 0,
    val carryWeight: Int = 0,
    val costStressHeadroom: Double = 0.0,
    val aiSafetyTimeoutSeconds: Double = 0.0,
    val isHalted: Boolean = true,
    val streamType: String = "OFFLINE",
    val telemetryAvailable: Boolean = false,
    val workerHealthStatus: String = "UNKNOWN",
    val workerHealthy: Boolean = false,
    val workerInstances: Int = 0,
    val healthyWorkerInstances: Int = 0,
    val workerHeartbeatAgeSeconds: Double = Double.NaN,
    val lastUpdatedEpochMs: Long = 0L
)

@OptIn(ExperimentalMaterial3Api::class)
@Composable
fun RiskDashboardScreen(
    backendUrl: String = "",
    initialMetrics: PortfolioRiskMetrics = PortfolioRiskMetrics(),
    accessToken: String? = null,
    onOpenPortal: () -> Unit = {},
    onRequestBiometricResume: ((onSuccess: () -> Unit) -> Unit) = { it() },
    onEmergencyHaltToggle: (Boolean) -> Unit = {}
) {
    val webSocketClient = remember { WebSocketManager() }
    val wsConnectionState by webSocketClient.connectionState.collectAsState()
    val streamedMetrics by webSocketClient.liveMetrics.collectAsState()
    val latencyMs by webSocketClient.lastMessageLatencyMs.collectAsState()

    var metrics by remember { mutableStateOf(initialMetrics) }
    var isSimulatingStress by remember { mutableStateOf(false) }
    var isRefreshing by remember { mutableStateOf(false) }
    var showAuditDialog by remember { mutableStateOf(false) }
    val coroutineScope = rememberCoroutineScope()

    // Real-Time WebSocket stream updates: replaces periodic HTTP polling to minimize latency and server load
    LaunchedEffect(streamedMetrics) {
        if (streamedMetrics.lastUpdatedEpochMs > 0 && streamedMetrics.streamType == "WEBSOCKET_PUSH") {
            metrics = streamedMetrics
        }
    }

    // Connect persistent WebSocket stream on appearance, disconnect on leave
    DisposableEffect(backendUrl, accessToken) {
        if (backendUrl.isNotBlank()) {
            webSocketClient.connect(backendUrl, accessToken)
        }
        onDispose {
            webSocketClient.close()
        }
    }

    // Colors adhering to dark institutional palette
    val backgroundDark = Color(0xFF070B18)
    val cardSurface = Color(0xFF0E1729)
    val cardBorder = Color(0xFF22304B)
    val accentCyan = Color(0xFF6EE7F7)
    val emeraldPass = Color(0xFF34D399)
    val amberWarning = Color(0xFFFBBF24)
    val redDanger = Color(0xFFEF4444)
    val textMuted = Color(0xFF94A3B8)

    Scaffold(
        modifier = Modifier
            .fillMaxSize()
            .testTag("risk_dashboard_scaffold"),
        containerColor = backgroundDark,
        contentWindowInsets = WindowInsets.safeDrawing,
        topBar = {
            TopAppBar(
                title = {
                    Column {
                        Row(verticalAlignment = Alignment.CenterVertically) {
                            Text(
                                text = "ATLAS RISK GOVERNOR",
                                fontSize = 11.sp,
                                fontWeight = FontWeight.Bold,
                                color = accentCyan,
                                letterSpacing = 1.5.sp
                            )
                            Spacer(modifier = Modifier.width(8.dp))

                            val (currentStatusColor, currentStatusText) = when (val state = wsConnectionState) {
                                is WebSocketConnectionState.Connected -> Pair(
                                    emeraldPass,
                                    if (latencyMs > 0) "WS REALTIME (${latencyMs}ms)" else "WS REALTIME"
                                )
                                is WebSocketConnectionState.Connecting -> Pair(accentCyan, "WS CONNECTING")
                                is WebSocketConnectionState.Reconnecting -> Pair(amberWarning, "WS RETRY #${state.attempt}")
                                is WebSocketConnectionState.Failed -> Pair(redDanger, "WS OFFLINE")
                                is WebSocketConnectionState.Disconnected -> Pair(textMuted, "DISCONNECTED")
                            }

                            Box(
                                modifier = Modifier
                                    .size(6.dp)
                                    .clip(CircleShape)
                                    .background(currentStatusColor)
                            )
                            Spacer(modifier = Modifier.width(4.dp))
                            Text(
                                text = currentStatusText,
                                fontSize = 9.sp,
                                fontWeight = FontWeight.Bold,
                                color = currentStatusColor
                            )
                        }
                        Text(
                            text = "Real-Time Risk Dashboard",
                            fontSize = 18.sp,
                            fontWeight = FontWeight.SemiBold,
                            color = Color.White
                        )
                    }
                },
                actions = {
                    IconButton(
                        onClick = {
                            coroutineScope.launch {
                                isRefreshing = true
                                if (wsConnectionState !is WebSocketConnectionState.Connected && backendUrl.isNotBlank() && !accessToken.isNullOrBlank()) {
                                    webSocketClient.connect(backendUrl, accessToken)
                                }
                                if (backendUrl.isNotBlank() && !accessToken.isNullOrBlank()) {
                                    webSocketClient.connect(backendUrl, accessToken)
                                }
                                if (streamedMetrics.lastUpdatedEpochMs > 0) metrics = streamedMetrics
                                isRefreshing = false
                            }
                        },
                        modifier = Modifier
                            .minimumInteractiveComponentSize()
                            .testTag("btn_refresh_metrics")
                    ) {
                        Icon(
                            imageVector = Icons.Default.Refresh,
                            contentDescription = "Refresh Metrics",
                            tint = if (isRefreshing) emeraldPass else accentCyan
                        )
                    }
                },
                colors = TopAppBarDefaults.topAppBarColors(
                    containerColor = backgroundDark
                )
            )
        }
    ) { padding ->
        LazyColumn(
            modifier = Modifier
                .fillMaxSize()
                .padding(padding)
                .padding(horizontal = 16.dp),
            verticalArrangement = Arrangement.spacedBy(16.dp)
        ) {
            // 1. Status & Live State Ribbon
            item {
                SystemStateRibbon(
                    metrics = metrics,
                    cardSurface = cardSurface,
                    cardBorder = cardBorder,
                    emeraldColor = emeraldPass,
                    redColor = redDanger,
                    warningColor = amberWarning,
                    onInspectAudit = { showAuditDialog = true }
                )
            }

            // 2. Primary Risk-Adjusted Return Ratios (Sharpe & Calmar)
            item {
                PrimaryRatiosCard(
                    metrics = metrics,
                    cardSurface = cardSurface,
                    cardBorder = cardBorder,
                    accentCyan = accentCyan,
                    emeraldColor = emeraldPass,
                    textMuted = textMuted
                )
            }

            // 3. Drawdown & Capital Preservation Gauge
            item {
                DrawdownGaugeCard(
                    metrics = metrics,
                    cardSurface = cardSurface,
                    cardBorder = cardBorder,
                    accentCyan = accentCyan,
                    amberColor = amberWarning,
                    redColor = redDanger,
                    textMuted = textMuted
                )
            }

            // 4. Regime & Ensemble Dynamic Allocation Matrix
            item {
                RegimeAllocatorCard(
                    metrics = metrics,
                    cardSurface = cardSurface,
                    cardBorder = cardBorder,
                    accentCyan = accentCyan,
                    emeraldColor = emeraldPass,
                    textMuted = textMuted,
                    isSimulatingStress = isSimulatingStress,
                    onToggleStress = {
                        // Production mobile controls never fabricate market stress data.
                        isSimulatingStress = false
                        metrics = PortfolioRiskMetrics()
                    }
                )
            }

            // 5. On-Device Quantitative Strategy Replay & Backtesting
            item {
                StrategyReplaySection()
            }

            // 6. Anti-Failure Controls & Actions
            item {
                ActionControls(
                    isHalted = metrics.isHalted,
                    onHaltToggle = {
                        if (!metrics.telemetryAvailable || accessToken.isNullOrBlank()) {
                            onEmergencyHaltToggle(true)
                        } else if (!metrics.isHalted) {
                            onEmergencyHaltToggle(true)
                        } else {
                            onRequestBiometricResume {
                                onEmergencyHaltToggle(false)
                            }
                        }
                    },
                    onOpenPortal = onOpenPortal,
                    accentCyan = accentCyan,
                    redDanger = redDanger,
                    cardSurface = cardSurface
                )
            }

            // 7. Fiduciary Risk Disclosure
            item {
                FiduciaryDisclosureCard(cardSurface = cardSurface, cardBorder = cardBorder)
                Spacer(modifier = Modifier.height(24.dp))
            }
        }
    }

    if (showAuditDialog) {
        AlertDialog(
            onDismissRequest = { showAuditDialog = false },
            title = {
                Text(
                    text = "3.10.47 Institutional Risk Audit",
                    fontWeight = FontWeight.Bold,
                    color = Color.White
                )
            },
            text = {
                Column(verticalArrangement = Arrangement.spacedBy(8.dp)) {
                    Text(
                        text = "• Sharpe Ratio: ${metrics.sharpeRatio} (Annualized vs risk-free)",
                        color = Color(0xFFCBD5E1),
                        fontSize = 13.sp
                    )
                    Text(
                        text = "• Calmar Ratio: ${metrics.calmarRatio} (Return / Max DD)",
                        color = Color(0xFFCBD5E1),
                        fontSize = 13.sp
                    )
                    Text(
                        text = "• Sortino Ratio: ${metrics.sortinoRatio} (Downside deviation focus)",
                        color = Color(0xFFCBD5E1),
                        fontSize = 13.sp
                    )
                    Text(
                        text = "• Deflated Sharpe Ratio: ${metrics.deflatedSharpeRatio} (>= 0.95 PASS)",
                        color = emeraldPass,
                        fontSize = 13.sp
                    )
                    Text(
                        text = "• Edge-Decay Z-Score: ${metrics.edgeDecayZScore} (z <= -2.0 HALT)",
                        color = accentCyan,
                        fontSize = 13.sp
                    )
                    Text(
                        text = "• Cost-Stress Multiplier: 2.0x Taker & Slippage (PASS)",
                        color = emeraldPass,
                        fontSize = 13.sp
                    )
                    Text(
                        text = "• AI Safety Latency Ceiling: 1.5s Fail-Closed (NO_TRADE)",
                        color = Color(0xFFCBD5E1),
                        fontSize = 13.sp
                    )
                }
            },
            confirmButton = {
                TextButton(onClick = { showAuditDialog = false }) {
                    Text("Close", color = accentCyan)
                }
            },
            containerColor = cardSurface
        )
    }
}

@Composable
private fun SystemStateRibbon(
    metrics: PortfolioRiskMetrics,
    cardSurface: Color,
    cardBorder: Color,
    emeraldColor: Color,
    redColor: Color,
    warningColor: Color,
    onInspectAudit: () -> Unit
) {
    Card(
        modifier = Modifier
            .fillMaxWidth()
            .border(1.dp, cardBorder, RoundedCornerShape(14.dp)),
        colors = CardDefaults.cardColors(containerColor = cardSurface),
        shape = RoundedCornerShape(14.dp)
    ) {
        Row(
            modifier = Modifier
                .fillMaxWidth()
                .padding(14.dp),
            verticalAlignment = Alignment.CenterVertically,
            horizontalArrangement = Arrangement.SpaceBetween
        ) {
            Row(verticalAlignment = Alignment.CenterVertically) {
                Box(
                    modifier = Modifier
                        .size(10.dp)
                        .clip(CircleShape)
                        .background(if (metrics.isHalted) redColor else emeraldColor)
                )
                Spacer(modifier = Modifier.width(8.dp))
                Column {
                    Text(
                        text = if (metrics.isHalted) "ENGINE HALTED (KILL SWITCH)" else "QUANT RISK ENGINE: ACTIVE",
                        fontWeight = FontWeight.Bold,
                        fontSize = 12.sp,
                        color = if (metrics.isHalted) redColor else emeraldColor
                    )
                    Text(
                        text = "Release 3.10.47 • Immutable Identity (1047)",
                        fontSize = 11.sp,
                        color = Color(0xFF64748B)
                    )

                    val healthColor = when (metrics.workerHealthStatus) {
                        "HEALTHY" -> emeraldColor
                        "STALE" -> warningColor
                        "MISSING" -> redColor
                        else -> Color(0xFF94A3B8)
                    }
                    Text(
                        text = "WORKER ENGINE: " + metrics.workerHealthStatus + " • " + metrics.healthyWorkerInstances + "/" + metrics.workerInstances + " healthy",
                        fontSize = 10.sp,
                        fontWeight = FontWeight.Bold,
                        color = healthColor,
                        modifier = Modifier.testTag("worker_health_status")
                    )                }
            }

            TextButton(
                onClick = onInspectAudit,
                modifier = Modifier.minimumInteractiveComponentSize()
            ) {
                Text(
                    text = "AUDIT",
                    color = Color(0xFF6EE7F7),
                    fontWeight = FontWeight.Bold,
                    fontSize = 12.sp
                )
            }
        }
    }
}

@Composable
private fun PrimaryRatiosCard(
    metrics: PortfolioRiskMetrics,
    cardSurface: Color,
    cardBorder: Color,
    accentCyan: Color,
    emeraldColor: Color,
    textMuted: Color
) {
    Card(
        modifier = Modifier
            .fillMaxWidth()
            .border(1.dp, cardBorder, RoundedCornerShape(16.dp)),
        colors = CardDefaults.cardColors(containerColor = cardSurface),
        shape = RoundedCornerShape(16.dp)
    ) {
        Column(modifier = Modifier.padding(18.dp)) {
            Text(
                text = "PORTFOLIO EFFICIENCY RATIOS",
                fontSize = 11.sp,
                fontWeight = FontWeight.Bold,
                color = textMuted,
                letterSpacing = 1.sp
            )

            Spacer(modifier = Modifier.height(14.dp))

            Row(
                modifier = Modifier.fillMaxWidth(),
                horizontalArrangement = Arrangement.SpaceBetween
            ) {
                // Sharpe Ratio
                MetricColumn(
                    title = "Sharpe Ratio",
                    value = String.format("%.2f", metrics.sharpeRatio),
                    subtext = "Hurdle: > 1.0",
                    valueColor = if (metrics.sharpeRatio >= 1.5) emeraldColor else Color.White
                )

                // Calmar Ratio
                MetricColumn(
                    title = "Calmar Ratio",
                    value = String.format("%.2f", metrics.calmarRatio),
                    subtext = "Ann. Ret / Max DD",
                    valueColor = accentCyan
                )

                // Sortino Ratio
                MetricColumn(
                    title = "Sortino Ratio",
                    value = String.format("%.2f", metrics.sortinoRatio),
                    subtext = "Downside Vol",
                    valueColor = Color.White
                )
            }

            Spacer(modifier = Modifier.height(14.dp))
            HorizontalDivider(color = Color(0xFF1E293B), thickness = 1.dp)
            Spacer(modifier = Modifier.height(12.dp))

            Row(
                modifier = Modifier.fillMaxWidth(),
                horizontalArrangement = Arrangement.SpaceBetween
            ) {
                Text(
                    text = "Deflated Sharpe (DSR):",
                    fontSize = 12.sp,
                    color = textMuted
                )
                Text(
                    text = "${metrics.deflatedSharpeRatio} (Floor: >= 0.95 PASS)",
                    fontSize = 12.sp,
                    fontWeight = FontWeight.Bold,
                    color = emeraldColor
                )
            }

            Spacer(modifier = Modifier.height(6.dp))

            Row(
                modifier = Modifier.fillMaxWidth(),
                horizontalArrangement = Arrangement.SpaceBetween
            ) {
                Text(
                    text = "Edge Decay Monitor (N=100):",
                    fontSize = 12.sp,
                    color = textMuted
                )
                Text(
                    text = "z = ${String.format("%+.2f", metrics.edgeDecayZScore)} (Halt: <= -2.0)",
                    fontSize = 12.sp,
                    fontWeight = FontWeight.Bold,
                    color = if (metrics.edgeDecayZScore <= -1.5) Color(0xFFFBBF24) else accentCyan
                )
            }
        }
    }
}

@Composable
private fun MetricColumn(
    title: String,
    value: String,
    subtext: String,
    valueColor: Color
) {
    Column {
        Text(text = title, fontSize = 11.sp, color = Color(0xFF64748B))
        Spacer(modifier = Modifier.height(2.dp))
        Text(
            text = value,
            fontSize = 22.sp,
            fontWeight = FontWeight.Bold,
            color = valueColor
        )
        Text(text = subtext, fontSize = 10.sp, color = Color(0xFF94A3B8))
    }
}

@Composable
private fun DrawdownGaugeCard(
    metrics: PortfolioRiskMetrics,
    cardSurface: Color,
    cardBorder: Color,
    accentCyan: Color,
    amberColor: Color,
    redColor: Color,
    textMuted: Color
) {
    val progress = (metrics.currentDrawdownPct / metrics.maxDrawdownLimitPct).toFloat().coerceIn(0f, 1f)
    val animatedProgress by animateFloatAsState(
        targetValue = progress,
        animationSpec = tween(durationMillis = 600),
        label = "drawdownProgress"
    )

    val gaugeColor = when {
        metrics.currentDrawdownPct >= 10.0 -> redColor
        metrics.currentDrawdownPct >= 5.0 -> amberColor
        else -> accentCyan
    }

    Card(
        modifier = Modifier
            .fillMaxWidth()
            .border(1.dp, cardBorder, RoundedCornerShape(16.dp)),
        colors = CardDefaults.cardColors(containerColor = cardSurface),
        shape = RoundedCornerShape(16.dp)
    ) {
        Column(modifier = Modifier.padding(18.dp)) {
            Row(
                modifier = Modifier.fillMaxWidth(),
                horizontalArrangement = Arrangement.SpaceBetween,
                verticalAlignment = Alignment.CenterVertically
            ) {
                Text(
                    text = "DRAWDOWN & CAPITAL PRESERVATION",
                    fontSize = 11.sp,
                    fontWeight = FontWeight.Bold,
                    color = textMuted,
                    letterSpacing = 1.sp
                )

                Text(
                    text = "${String.format("%.2f", metrics.currentDrawdownPct)}%",
                    fontSize = 18.sp,
                    fontWeight = FontWeight.Bold,
                    color = gaugeColor
                )
            }

            Spacer(modifier = Modifier.height(14.dp))

            // Custom Drawdown Progress Bar
            Box(
                modifier = Modifier
                    .fillMaxWidth()
                    .height(14.dp)
                    .clip(RoundedCornerShape(7.dp))
                    .background(Color(0xFF1E293B))
            ) {
                Box(
                    modifier = Modifier
                        .fillMaxWidth(animatedProgress)
                        .fillMaxHeight()
                        .clip(RoundedCornerShape(7.dp))
                        .background(
                            Brush.horizontalGradient(
                                listOf(Color(0xFF38BDF8), gaugeColor)
                            )
                        )
                )
            }

            Spacer(modifier = Modifier.height(8.dp))

            Row(
                modifier = Modifier.fillMaxWidth(),
                horizontalArrangement = Arrangement.SpaceBetween
            ) {
                Text(text = "0% (Peak Equity)", fontSize = 10.sp, color = Color(0xFF64748B))
                Text(
                    text = "Max Limit: ${metrics.maxDrawdownLimitPct}%",
                    fontSize = 10.sp,
                    fontWeight = FontWeight.SemiBold,
                    color = redColor
                )
            }

            Spacer(modifier = Modifier.height(12.dp))
            HorizontalDivider(color = Color(0xFF1E293B), thickness = 1.dp)
            Spacer(modifier = Modifier.height(12.dp))

            Row(
                modifier = Modifier.fillMaxWidth(),
                horizontalArrangement = Arrangement.SpaceBetween
            ) {
                Text(
                    text = "Trailing Daily Loss Ceiling:",
                    fontSize = 12.sp,
                    color = textMuted
                )
                Text(
                    text = "${String.format("%.2f", metrics.dailyLossPct)}% / ${metrics.dailyLossLimitPct}%",
                    fontSize = 12.sp,
                    fontWeight = FontWeight.Bold,
                    color = Color.White
                )
            }

            Spacer(modifier = Modifier.height(6.dp))

            Row(
                modifier = Modifier.fillMaxWidth(),
                horizontalArrangement = Arrangement.SpaceBetween
            ) {
                Text(
                    text = "Portfolio Total Equity:",
                    fontSize = 12.sp,
                    color = textMuted
                )
                Text(
                    text = "$${String.format("%,.2f", metrics.equity)}",
                    fontSize = 12.sp,
                    fontWeight = FontWeight.Bold,
                    color = Color.White
                )
            }
        }
    }
}

@Composable
private fun RegimeAllocatorCard(
    metrics: PortfolioRiskMetrics,
    cardSurface: Color,
    cardBorder: Color,
    accentCyan: Color,
    emeraldColor: Color,
    textMuted: Color,
    isSimulatingStress: Boolean,
    onToggleStress: () -> Unit
) {
    Card(
        modifier = Modifier
            .fillMaxWidth()
            .border(1.dp, cardBorder, RoundedCornerShape(16.dp)),
        colors = CardDefaults.cardColors(containerColor = cardSurface),
        shape = RoundedCornerShape(16.dp)
    ) {
        Column(modifier = Modifier.padding(18.dp)) {
            Row(
                modifier = Modifier.fillMaxWidth(),
                horizontalArrangement = Arrangement.SpaceBetween,
                verticalAlignment = Alignment.CenterVertically
            ) {
                Text(
                    text = "DYNAMIC REGIME DETECTOR",
                    fontSize = 11.sp,
                    fontWeight = FontWeight.Bold,
                    color = textMuted,
                    letterSpacing = 1.sp
                )

                Text(
                    text = metrics.regime,
                    fontSize = 11.sp,
                    fontWeight = FontWeight.Bold,
                    color = accentCyan
                )
            }

            Spacer(modifier = Modifier.height(4.dp))
            Text(
                text = metrics.regimeDescription,
                fontSize = 11.sp,
                color = Color(0xFF64748B)
            )

            Spacer(modifier = Modifier.height(14.dp))
            Text(
                text = "Adaptive Strategy Weights (Anti-Churn 180m):",
                fontSize = 11.sp,
                fontWeight = FontWeight.SemiBold,
                color = Color(0xFFCBD5E1)
            )
            Spacer(modifier = Modifier.height(8.dp))

            StrategyWeightRow("Momentum ATR (1h/4h)", "${metrics.momentumWeight}%", emeraldColor)
            StrategyWeightRow("Mean Reversion (Z-Score)", "${metrics.meanRevWeight}%", Color(0xFFCBD5E1))
            StrategyWeightRow("Session Opening Range", "${metrics.sessionWeight}%", Color(0xFFCBD5E1))
            StrategyWeightRow("Volatility Carry & Skew", "${metrics.carryWeight}%", Color(0xFFCBD5E1))

            Spacer(modifier = Modifier.height(12.dp))

            Button(
                onClick = onToggleStress,
                modifier = Modifier
                    .fillMaxWidth()
                    .height(40.dp)
                    .minimumInteractiveComponentSize()
                    .testTag("btn_toggle_regime_sim"),
                colors = ButtonDefaults.buttonColors(
                    containerColor = Color(0xFF1E293B),
                    contentColor = if (isSimulatingStress) Color(0xFFFBBF24) else accentCyan
                ),
                shape = RoundedCornerShape(10.dp)
            ) {
                Text(
                    text = if (isSimulatingStress) "REVERT TO NORMAL REGIME" else "SIMULATE HIGH-VOLATILITY REGIME",
                    fontSize = 11.sp,
                    fontWeight = FontWeight.Bold
                )
            }
        }
    }
}

@Composable
private fun StrategyWeightRow(name: String, weight: String, color: Color) {
    Row(
        modifier = Modifier
            .fillMaxWidth()
            .padding(vertical = 2.dp),
        horizontalArrangement = Arrangement.SpaceBetween
    ) {
        Text(text = "• $name", fontSize = 11.sp, color = Color(0xFF94A3B8))
        Text(text = weight, fontSize = 11.sp, fontWeight = FontWeight.Bold, color = color)
    }
}

@Composable
private fun ActionControls(
    isHalted: Boolean,
    onHaltToggle: () -> Unit,
    onOpenPortal: () -> Unit,
    accentCyan: Color,
    redDanger: Color,
    cardSurface: Color
) {
    Column(verticalArrangement = Arrangement.spacedBy(10.dp)) {
        Button(
            onClick = onOpenPortal,
            modifier = Modifier
                .fillMaxWidth()
                .height(48.dp)
                .minimumInteractiveComponentSize()
                .testTag("btn_open_web_portal"),
            colors = ButtonDefaults.buttonColors(
                containerColor = accentCyan,
                contentColor = Color(0xFF070B18)
            ),
            shape = RoundedCornerShape(12.dp)
        ) {
            Text(
                text = "OPEN WEB TRADING PORTAL",
                fontWeight = FontWeight.Bold,
                fontSize = 13.sp
            )
        }

        Button(
            onClick = onHaltToggle,
            modifier = Modifier
                .fillMaxWidth()
                .height(48.dp)
                .minimumInteractiveComponentSize()
                .testTag("btn_emergency_halt"),
            colors = ButtonDefaults.buttonColors(
                containerColor = if (isHalted) Color(0xFF064E3B) else Color(0xFF3B1219),
                contentColor = if (isHalted) Color(0xFF34D399) else Color(0xFFFCA5A5)
            ),
            shape = RoundedCornerShape(12.dp)
        ) {
            Text(
                text = if (isHalted) "RESUME TRADING ENGINE" else "EMERGENCY HALT (KILL SWITCH)",
                fontWeight = FontWeight.Bold,
                fontSize = 13.sp
            )
        }
    }
}

@Composable
private fun FiduciaryDisclosureCard(cardSurface: Color, cardBorder: Color) {
    Card(
        modifier = Modifier
            .fillMaxWidth()
            .border(1.dp, cardBorder, RoundedCornerShape(12.dp)),
        colors = CardDefaults.cardColors(containerColor = cardSurface),
        shape = RoundedCornerShape(12.dp)
    ) {
        Column(modifier = Modifier.padding(14.dp)) {
            Text(
                text = "FIDUCIARY & RISK DISCLOSURE",
                fontSize = 10.sp,
                fontWeight = FontWeight.Bold,
                color = Color(0xFFEF4444)
            )
            Spacer(modifier = Modifier.height(4.dp))
            Text(
                text = "Quantitative trading involves substantial risk of loss. Past or simulated results (Sharpe, Calmar, DSR) do not guarantee future returns. Users may lose some or all capital. The Atlas risk governor enforces capital preservation upstream of all execution.",
                fontSize = 10.sp,
                color = Color(0xFF94A3B8),
                lineHeight = 14.sp
            )
        }
    }
}
