package com.atlas.trading.network

import android.util.Log
import com.atlas.trading.ui.PortfolioRiskMetrics
import kotlinx.coroutines.*
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.StateFlow
import kotlinx.coroutines.flow.asStateFlow
import okhttp3.*
import org.json.JSONObject
import java.util.concurrent.TimeUnit
import kotlin.math.min

sealed class WebSocketConnectionState {
    object Disconnected : WebSocketConnectionState()
    object Connecting : WebSocketConnectionState()
    data class Connected(val url: String, val connectedAtEpochMs: Long = System.currentTimeMillis()) : WebSocketConnectionState()
    data class Reconnecting(val attempt: Int, val nextDelayMs: Long) : WebSocketConnectionState()
    data class Failed(val error: String) : WebSocketConnectionState()
}

/**
 * Real-Time WebSocket client for receiving sub-second live portfolio equity & risk telemetry.
 * Completely replaces HTTP polling to minimize network latency, bandwidth, and server load.
 */
class LiveEquityWebSocketClient(
    private val client: OkHttpClient = OkHttpClient.Builder()
        .readTimeout(0, TimeUnit.MILLISECONDS) // Disable socket timeout for long-lived WebSocket
        .pingInterval(15, TimeUnit.SECONDS)   // Send automated WebSocket frame pings
        .build(),
    private val coroutineScope: CoroutineScope = CoroutineScope(Dispatchers.IO + SupervisorJob())
) {
    private val _connectionState = MutableStateFlow<WebSocketConnectionState>(WebSocketConnectionState.Disconnected)
    val connectionState: StateFlow<WebSocketConnectionState> = _connectionState.asStateFlow()

    private val _liveMetrics = MutableStateFlow(PortfolioRiskMetrics())
    val liveMetrics: StateFlow<PortfolioRiskMetrics> = _liveMetrics.asStateFlow()

    private val _lastMessageLatencyMs = MutableStateFlow(0L)
    val lastMessageLatencyMs: StateFlow<Long> = _lastMessageLatencyMs.asStateFlow()

    private var activeWebSocket: WebSocket? = null
    private var reconnectJob: Job? = null
    private var pingJob: Job? = null
    private var reconnectAttempts = 0
    private var currentUrl: String = ""
    private var isIntentionalClose = false

    fun connect(baseUrl: String) {
        if (baseUrl.isBlank()) return
        val targetWsUrl = buildWebSocketUrl(baseUrl)
        if (currentUrl == targetWsUrl && _connectionState.value is WebSocketConnectionState.Connected) {
            return
        }

        currentUrl = targetWsUrl
        isIntentionalClose = false
        reconnectAttempts = 0
        initiateConnection(targetWsUrl)
    }

    private fun initiateConnection(wsUrl: String) {
        reconnectJob?.cancel()
        _connectionState.value = WebSocketConnectionState.Connecting

        val request = Request.Builder()
            .url(wsUrl)
            .header("Sec-WebSocket-Protocol", "atlas-telemetry-v1")
            .build()

        activeWebSocket?.cancel()
        activeWebSocket = client.newWebSocket(request, createSocketListener(wsUrl))
    }

    private fun createSocketListener(targetUrl: String): WebSocketListener {
        return object : WebSocketListener() {
            override fun onOpen(webSocket: WebSocket, response: Response) {
                reconnectAttempts = 0
                _connectionState.value = WebSocketConnectionState.Connected(targetUrl)
                startHeartbeatPing(webSocket)
            }

            override fun onMessage(webSocket: WebSocket, text: String) {
                val now = System.currentTimeMillis()
                val parsed = parseEquityMessage(text, _liveMetrics.value)
                if (parsed != null) {
                    val serverTs = parsed.lastUpdatedEpochMs
                    val delta = if (serverTs > 0 && now >= serverTs) now - serverTs else 0L
                    _lastMessageLatencyMs.value = delta
                    _liveMetrics.value = parsed.copy(streamType = "WEBSOCKET_PUSH")
                }
            }

            override fun onClosing(webSocket: WebSocket, code: Int, reason: String) {
                webSocket.close(1000, null)
            }

            override fun onClosed(webSocket: WebSocket, code: Int, reason: String) {
                stopHeartbeatPing()
                if (!isIntentionalClose) {
                    scheduleReconnect()
                } else {
                    _connectionState.value = WebSocketConnectionState.Disconnected
                }
            }

            override fun onFailure(webSocket: WebSocket, t: Throwable, response: Response?) {
                stopHeartbeatPing()
                val errMsg = t.message ?: "Connection failure"
                _connectionState.value = WebSocketConnectionState.Failed(errMsg)
                if (!isIntentionalClose) {
                    scheduleReconnect()
                }
            }
        }
    }

    private fun scheduleReconnect() {
        if (currentUrl.isBlank()) return
        reconnectAttempts++
        // Exponential backoff: 1s, 2s, 4s, 8s, up to 15s max
        val delayMs = min(15_000L, 1_000L * (1 shl min(4, reconnectAttempts - 1)))
        _connectionState.value = WebSocketConnectionState.Reconnecting(reconnectAttempts, delayMs)

        reconnectJob?.cancel()
        reconnectJob = coroutineScope.launch {
            delay(delayMs)
            if (!isIntentionalClose && currentUrl.isNotBlank()) {
                initiateConnection(currentUrl)
            }
        }
    }

    private fun startHeartbeatPing(socket: WebSocket) {
        pingJob?.cancel()
        pingJob = coroutineScope.launch {
            while (isActive) {
                delay(12_000)
                try {
                    socket.send("ping")
                } catch (_: Exception) {}
            }
        }
    }

    private fun stopHeartbeatPing() {
        pingJob?.cancel()
        pingJob = null
    }

    fun disconnect() {
        isIntentionalClose = true
        reconnectJob?.cancel()
        stopHeartbeatPing()
        activeWebSocket?.close(1000, "Normal closure")
        activeWebSocket = null
        _connectionState.value = WebSocketConnectionState.Disconnected
    }

    companion object {
        fun buildWebSocketUrl(rawUrl: String): String {
            val trimmed = rawUrl.trim().trimEnd('/')
            val wsScheme = when {
                trimmed.startsWith("https://", ignoreCase = true) -> "wss://" + trimmed.substring(8)
                trimmed.startsWith("http://", ignoreCase = true) -> "ws://" + trimmed.substring(7)
                trimmed.startsWith("wss://", ignoreCase = true) || trimmed.startsWith("ws://", ignoreCase = true) -> trimmed
                else -> "wss://$trimmed"
            }
            return if (wsScheme.endsWith("/ws/risk-telemetry") || wsScheme.endsWith("/ws/equity")) {
                wsScheme
            } else {
                "$wsScheme/ws/risk-telemetry"
            }
        }

        fun parseEquityMessage(jsonString: String, current: PortfolioRiskMetrics): PortfolioRiskMetrics? {
            return try {
                val json = JSONObject(jsonString)
                if (!json.has("equity")) return null
                PortfolioRiskMetrics(
                    equity = json.optDouble("equity", current.equity),
                    peakEquity = json.optDouble("peak_equity", current.peakEquity),
                    realizedPnl = json.optDouble("realized_pnl", current.realizedPnl),
                    unrealizedPnl = json.optDouble("unrealized_pnl", current.unrealizedPnl),
                    currentDrawdownPct = json.optDouble("current_drawdown_pct", current.currentDrawdownPct),
                    maxDrawdownLimitPct = json.optDouble("max_drawdown_limit_pct", current.maxDrawdownLimitPct),
                    dailyLossPct = json.optDouble("daily_loss_pct", current.dailyLossPct),
                    dailyLossLimitPct = json.optDouble("daily_loss_limit_pct", current.dailyLossLimitPct),
                    sharpeRatio = json.optDouble("sharpe_ratio", current.sharpeRatio),
                    calmarRatio = json.optDouble("calmar_ratio", current.calmarRatio),
                    sortinoRatio = json.optDouble("sortino_ratio", current.sortinoRatio),
                    deflatedSharpeRatio = json.optDouble("deflated_sharpe_ratio", current.deflatedSharpeRatio),
                    edgeDecayZScore = json.optDouble("edge_decay_z_score", current.edgeDecayZScore),
                    regime = json.optString("regime", current.regime),
                    regimeDescription = json.optString("regime_description", current.regimeDescription),
                    momentumWeight = json.optInt("momentum_weight", current.momentumWeight),
                    meanRevWeight = json.optInt("mean_rev_weight", current.meanRevWeight),
                    sessionWeight = json.optInt("session_weight", current.sessionWeight),
                    carryWeight = json.optInt("carry_weight", current.carryWeight),
                    costStressHeadroom = json.optDouble("cost_stress_headroom", current.costStressHeadroom),
                    aiSafetyTimeoutSeconds = json.optDouble("ai_safety_timeout_seconds", current.aiSafetyTimeoutSeconds),
                    isHalted = json.optBoolean("is_halted", current.isHalted),
                    streamType = json.optString("stream_type", "WEBSOCKET_PUSH"),
                    lastUpdatedEpochMs = json.optLong("last_updated_epoch_ms", System.currentTimeMillis())
                )
            } catch (_: Exception) {
                null
            }
        }
    }
}
