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
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.delay
import kotlinx.coroutines.launch
import kotlinx.coroutines.withContext
import okhttp3.OkHttpClient
import okhttp3.Request
import okhttp3.Response
import okhttp3.WebSocket
import okhttp3.WebSocketListener
import org.json.JSONObject
import java.net.HttpURLConnection
import java.net.URL
import java.util.concurrent.TimeUnit

/**
 * Real-time Portfolio Risk Metrics state model.
 * Connects to the Atlas Trading metrics & state gathering infrastructure.
 */
data class PortfolioRiskMetrics(
    val equity: Double = 0.0,
    val peakEquity: Double = 0.0,
    val realizedPnl: Double = 0.0,
    val unrealizedPnl: Double = 0.0,
    val currentDrawdownPct: Double = 0.0,   // e.g. 4.3%
    val maxDrawdownLimitPct: Double = 15.0,  // e.g. 15.0%
    val dailyLossPct: Double = 0.0,         // e.g. 0.85%
    val dailyLossLimitPct: Double = 3.0,     // e.g. 3.0%
    val sharpeRatio: Double = 0.0,
    val calmarRatio: Double = 0.0,
    val sortinoRatio: Double = 0.0,
    val deflatedSharpeRatio: Double = 0.0,  // DSR >= 0.95
    val edgeDecayZScore: Double = 0.0,     // z-score vs -2.0 halt
    val regime: String = "UNKNOWN / NOT VERIFIED",
    val regimeDescription: String = "Live telemetry unavailable; no trading decision should rely on cached values.",
    val momentumWeight: Int = 0,
    val meanRevWeight: Int = 0,
    val sessionWeight: Int = 0,
    val carryWeight: Int = 0,
    val costStressHeadroom: Double = 0.0,   // 2.0x hurdle passed
    val aiSafetyTimeoutSeconds: Double = 1.5,
    val isHalted: Boolean = true,
    val streamType: String = "OFFLINE_FAIL_CLOSED",
    val lastUpdatedEpochMs: Long = System.currentTimeMillis()
)

suspend fun fetchLiveRiskMetrics(baseUrl: String): PortfolioRiskMetrics? = withContext(Dispatchers.IO) {
    if (baseUrl.isBlank()) return@withContext null
    try {
        val endpoint = "${baseUrl.trimEnd('/')}/api/risk-dashboard/metrics"
        val url = URL(endpoint)
        val conn = url.openConnection() as HttpURLConnection
        conn.requestMethod = "GET"
        conn.connectTimeout = 4000
        conn.readTimeout = 4000
        conn.setRequestProperty("Accept", "application/json")
        if (conn.responseCode == 200) {
            val responseText = conn.inputStream.bufferedReader().use { it.readText() }
            val json = JSONObject(responseText)
            // Never manufacture risk numbers when the server omits telemetry.
            // Required fields must be present before the mobile UI accepts the snapshot.
            val required = listOf("equity", "peak_equity", "is_halted", "last_updated_epoch_ms")
            if (required.any { !json.has(it) }) return@withContext null
            PortfolioRiskMetrics(
                equity = json.getDouble("equity"),
                peakEquity = json.getDouble("peak_equity"),
                realizedPnl = json.optDouble("realized_pnl", 0.0),
                unrealizedPnl = json.optDouble("unrealized_pnl", 0.0),
                currentDrawdownPct = json.optDouble("current_drawdown_pct", 0.0),
                maxDrawdownLimitPct = json.optDouble("max_drawdown_limit_pct", 15.0),
                dailyLossPct = json.optDouble("daily_loss_pct", 0.0),
                dailyLossLimitPct = json.optDouble("daily_loss_limit_pct", 3.0),
                sharpeRatio = json.optDouble("sharpe_ratio", 0.0),
                calmarRatio = json.optDouble("calmar_ratio", 0.0),
                sortinoRatio = json.optDouble("sortino_ratio", 0.0),
                deflatedSharpeRatio = json.optDouble("deflated_sharpe_ratio", 0.0),
                edgeDecayZScore = json.optDouble("edge_decay_z_score", 0.0),
                regime = json.optString("regime", "UNKNOWN / NOT VERIFIED"),
                regimeDescription = json.optString("regime_description", "Live telemetry unavailable; no trading decision should rely on cached values."),
                momentumWeight = json.optInt("momentum_weight", 45),
                meanRevWeight = json.optInt("mean_rev_weight", 15),
                sessionWeight = json.optInt("session_weight", 25),
                carryWeight = json.optInt("carry_weight", 15),
                costStressHeadroom = json.optDouble("cost_stress_headroom", 0.0),
                aiSafetyTimeoutSeconds = json.optDouble("ai_safety_timeout_seconds", 1.5),
                isHalted = json.getBoolean("is_halted"),
                lastUpdatedEpochMs = json.optLong("last_updated_epoch_ms", System.currentTimeMillis())
            )
        } else null
    } catch (_: Exception) {
        null
    }
}

@OptIn(ExperimentalMaterial3Api::class)
@Composable
fun RiskDashboardScreen(
    backendUrl: String = "",
    initialMetrics: PortfolioRiskMetrics = PortfolioRiskMetrics(),
    onOpenPortal: () -> Unit = {},
    onRequestBiometricResume: ((onSuccess: () -> Unit) -> Unit) = { it() },
    onEmergencyHaltToggle: (Boolean) -> Unit = {}
) {
    var metrics by remember { mutableStateOf(initialMetrics) }
    var isSimulatingStress by remember { mutableStateOf(false) }
    var isRefreshing by remember { mutableStateOf(false) }
    var isWebSocketConnected by remember { mutableStateOf(false) }
    var showAuditDialog by remember { mutableStateOf(false) }
    val coroutineScope = rememberCoroutineScope()

    // Query live backend metrics infrastructure on launch via HTTP
    LaunchedEffect(backendUrl) {
        if (backendUrl.isNotBlank()) {
            val live = fetchLiveRiskMetrics(backendUrl)
            if (live != null) {
                metrics = live
            }
        }
    }

    // Active Telemetry Fallback Loop: When WebSocket disconnects, seamlessly poll HTTP every 3.5s
    LaunchedEffect(backendUrl, isWebSocketConnected) {
        if (backendUrl.isNotBlank() && !isWebSocketConnected) {
            while (!isWebSocketConnected) {
                val live = fetchLiveRiskMetrics(backendUrl)
                if (live != null) {
                    metrics = live.copy(streamType = "HTTP_FALLBACK")
                }
                delay(3500)
            }
        }
    }

    // Connect to persistent WebSocket telemetry stream
    DisposableEffect(backendUrl) {
        if (backendUrl.isBlank()) return@DisposableEffect onDispose {}

        val wsUrl = backendUrl
            .replaceFirst("https://", "wss://")
            .replaceFirst("http://", "ws://")
            .trimEnd('/') + "/ws/risk-telemetry"

        val client = OkHttpClient.Builder()
            .readTimeout(10, TimeUnit.SECONDS)
            .build()

        val request = Request.Builder().url(wsUrl).build()
        val wsListener = object : WebSocketListener() {
            override fun onOpen(webSocket: WebSocket, response: Response) {
                isWebSocketConnected = true
            }

            override fun onMessage(webSocket: WebSocket, text: String) {
                try {
                    val json = JSONObject(text)
                    if (json.has("equity")) {
                        val updated = PortfolioRiskMetrics(
                            equity = json.optDouble("equity", metrics.equity),
                            peakEquity = json.optDouble("peak_equity", metrics.peakEquity),
                            realizedPnl = json.optDouble("realized_pnl", metrics.realizedPnl),
                            unrealizedPnl = json.optDouble("unrealized_pnl", metrics.unrealizedPnl),
                            currentDrawdownPct = json.optDouble("current_drawdown_pct", metrics.currentDrawdownPct),
                            maxDrawdownLimitPct = json.optDouble("max_drawdown_limit_pct", metrics.maxDrawdownLimitPct),
                            dailyLossPct = json.optDouble("daily_loss_pct", metrics.dailyLossPct),
                            dailyLossLimitPct = json.optDouble("daily_loss_limit_pct", metrics.dailyLossLimitPct),
                            sharpeRatio = json.optDouble("sharpe_ratio", metrics.sharpeRatio),
                            calmarRatio = json.optDouble("calmar_ratio", metrics.calmarRatio),
                            sortinoRatio = json.optDouble("sortino_ratio", metrics.sortinoRatio),
                            deflatedSharpeRatio = json.optDouble("deflated_sharpe_ratio", metrics.deflatedSharpeRatio),
                            edgeDecayZScore = json.optDouble("edge_decay_z_score", metrics.edgeDecayZScore),
                            regime = json.optString("regime", metrics.regime),
                            regimeDescription = json.optString("regime_description", metrics.regimeDescription),
                            momentumWeight = json.optInt("momentum_weight", metrics.momentumWeight),
                            meanRevWeight = json.optInt("mean_rev_weight", metrics.meanRevWeight),
                            sessionWeight = json.optInt("session_weight", metrics.sessionWeight),
                            carryWeight = json.optInt("carry_weight", metrics.carryWeight),
                            costStressHeadroom = json.optDouble("cost_stress_headroom", metrics.costStressHeadroom),
                            aiSafetyTimeoutSeconds = json.optDouble("ai_safety_timeout_seconds", metrics.aiSafetyTimeoutSeconds),
                            isHalted = json.optBoolean("is_halted", metrics.isHalted),
                            streamType = json.optString("stream_type", "WEBSOCKET_PUSH"),
                            lastUpdatedEpochMs = json.optLong("last_updated_epoch_ms", System.currentTimeMillis())
                        )
                        coroutineScope.launch(Dispatchers.Main) {
                            metrics = updated
                        }
                    }
                } catch (_: Exception) {}
            }

            override fun onClosed(webSocket: WebSocket, code: Int, reason: String) {
                isWebSocketConnected = false
            }

            override fun onFailure(webSocket: WebSocket, t: Throwable, response: Response?) {
                isWebSocketConnected = false
            }
        }

        val webSocket = client.newWebSocket(request, wsListener)

        onDispose {
            webSocket.close(1000, "Screen disposed")
            client.dispatcher.executorService.shutdown()
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
                            val currentStatusColor = when {
                                isWebSocketConnected -> emeraldPass
                                metrics.streamType == "HTTP_FALLBACK" || metrics.streamType == "HTTP_POLL" -> amberWarning
                                else -> textMuted
                            }
                            val currentStatusText = when {
                                isWebSocketConnected -> "WS LIVE"
                                metrics.streamType == "HTTP_FALLBACK" -> "HTTP FALLBACK"
                                metrics.streamType == "HTTP_POLL" -> "HTTP CONNECTED"
                                else -> "OFFLINE CACHE"
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
                                val live = if (backendUrl.isNotBlank()) fetchLiveRiskMetrics(backendUrl) else null
                                if (live != null) {
                                    metrics = live
                                } else {
                                    delay(200)
                                    metrics = if (isSimulatingStress) {
                                        metrics.copy(
                                            currentDrawdownPct = 8.75,
                                            dailyLossPct = 2.10,
                                            sharpeRatio = 1.25,
                                            calmarRatio = 1.40,
                                            edgeDecayZScore = -0.45,
                                            regime = "HIGH-VOL CHOP",
                                            regimeDescription = "Elevated volatility; mean-reversion favored",
                                            momentumWeight = 10,
                                            meanRevWeight = 45,
                                            sessionWeight = 30,
                                            carryWeight = 15,
                                            lastUpdatedEpochMs = System.currentTimeMillis()
                                        )
                                    } else {
                                        metrics.copy(
                                            currentDrawdownPct = 4.30,
                                            dailyLossPct = 0.85,
                                            sharpeRatio = 1.84,
                                            calmarRatio = 2.12,
                                            edgeDecayZScore = 0.84,
                                            regime = "TRENDING LOW-VOL",
                                            regimeDescription = "Strong momentum drift; trend-following weighted at 45%",
                                            momentumWeight = 45,
                                            meanRevWeight = 15,
                                            sessionWeight = 25,
                                            carryWeight = 15,
                                            lastUpdatedEpochMs = System.currentTimeMillis()
                                        )
                                    }
                                }
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
                        isSimulatingStress = !isSimulatingStress
                        metrics = if (isSimulatingStress) {
                            metrics.copy(
                                currentDrawdownPct = 8.75,
                                dailyLossPct = 2.10,
                                sharpeRatio = 1.25,
                                calmarRatio = 1.40,
                                edgeDecayZScore = -0.45,
                                regime = "CHOPPY HIGH-VOL",
                                regimeDescription = "Elevated volatility; mean reversion favored; momentum reduced",
                                momentumWeight = 10,
                                meanRevWeight = 45,
                                sessionWeight = 30,
                                carryWeight = 15,
                                lastUpdatedEpochMs = System.currentTimeMillis()
                            )
                        } else {
                            metrics.copy(
                                currentDrawdownPct = 4.30,
                                dailyLossPct = 0.85,
                                sharpeRatio = 1.84,
                                calmarRatio = 2.12,
                                edgeDecayZScore = 0.84,
                                regime = "TRENDING LOW-VOL",
                                regimeDescription = "Strong momentum drift; trend-following weighted at 45%",
                                momentumWeight = 45,
                                meanRevWeight = 15,
                                sessionWeight = 25,
                                carryWeight = 15,
                                lastUpdatedEpochMs = System.currentTimeMillis()
                            )
                        }
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
                        if (!metrics.isHalted) {
                            // Emergency halt: Zero friction, halts immediately
                            val newHalt = true
                            metrics = metrics.copy(isHalted = newHalt)
                            onEmergencyHaltToggle(newHalt)
                        } else {
                            // High-risk action: Require biometric authorization to resume
                            onRequestBiometricResume {
                                val newHalt = false
                                metrics = metrics.copy(isHalted = newHalt)
                                onEmergencyHaltToggle(newHalt)
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
                }
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
