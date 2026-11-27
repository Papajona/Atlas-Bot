package com.atlas.trading.ui

import androidx.compose.animation.AnimatedVisibility
import androidx.compose.foundation.Canvas
import androidx.compose.foundation.background
import androidx.compose.foundation.border
import androidx.compose.foundation.clickable
import androidx.compose.foundation.gestures.detectDragGestures
import androidx.compose.foundation.gestures.detectTapGestures
import androidx.compose.foundation.layout.*
import androidx.compose.foundation.rememberScrollState
import androidx.compose.foundation.verticalScroll
import androidx.compose.foundation.text.KeyboardOptions
import androidx.compose.ui.text.input.KeyboardType
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
import com.atlas.trading.network.CustomerDataClient
import com.atlas.trading.auth.CustomerAuthClient
import kotlinx.coroutines.launch
import kotlin.math.max
import kotlin.math.min

/**
 * Parse ISO-8601 server timestamps with either a Z suffix or an explicit UTC offset.
 *
 * OffsetDateTime handles Python datetime.isoformat() output such as +00:00 across
 * Android's supported java.time implementations; Instant.parse is less portable
 * for that representation on older Android runtimes.
 */
internal fun parseEquityObservationEpochMillis(value: String): Long? =
    runCatching {
        java.time.OffsetDateTime.parse(value).toInstant().toEpochMilli()
    }.getOrNull()

/** Keep a scrub selection inside the current timeframe's point list. */
internal fun safeEquityChartSelectedIndex(selectedIndex: Int?, pointCount: Int): Int? {
    if (pointCount <= 0) return null
    return (selectedIndex ?: pointCount - 1).coerceIn(0, pointCount - 1)
}

/**
 * Historical daily equity and profit data point for 30-day visualization.
 */
data class EquityPoint(
    val day: Int,
    val dateLabel: String,
    val equity: Double,
    val dailyPnl: Double,
    val cumProfit: Double,
    val observedAtEpochMillis: Long
)

enum class DashboardTab {
    HOME,
    PORTFOLIO,
    MARKET,
    SETTINGS
}

enum class AssetClass(
    val label: String,
    val badge: String,
    val scannerAssetClass: String
) {
    ALL("All Markets", "🌐", "crypto"),
    CRYPTO("Crypto", "🪙", "crypto"),
    FOREX("Forex", "💱", "forex"),
    COMMODITIES("Commodities", "🥇", "commodity")
}

@OptIn(ExperimentalMaterial3Api::class)
@Composable
fun UserDashboardScreen(
    userName: String = "Account",
    backendUrl: String = "",
    customerAccessToken: String? = null,
    onOpenOperatorConsole: () -> Unit = {},
    onOpenPortal: () -> Unit = {}
) {
    var currentTab by remember { mutableStateOf(DashboardTab.HOME) }
    var selectedAssetClass by remember { mutableStateOf(AssetClass.ALL) }
    var selectedTimeframeDays by remember { mutableIntStateOf(30) } // 7 or 30 days
    var chartCrosshairEnabled by remember { mutableStateOf(true) }
    var actionDialogMessage by remember { mutableStateOf<String?>(null) }
    var showPaperTradeDialog by remember { mutableStateOf(false) }
    var paperOrders by remember { mutableStateOf<List<CustomerDataClient.PaperOrder>>(emptyList()) }
    var paperBalances by remember { mutableStateOf<List<CustomerDataClient.PaperBalance>>(emptyList()) }
    var paperOrdersBusy by remember { mutableStateOf(false) }
    var paperOrderBusy by remember { mutableStateOf(false) }
    var paperOrderFeedback by remember { mutableStateOf<String?>(null) }
    var paperOrderError by remember { mutableStateOf<String?>(null) }
    var paperOrdersRefreshKey by remember { mutableIntStateOf(0) }
    var activeCustomerToken by remember { mutableStateOf(customerAccessToken) }
    var customerSnapshot by remember { mutableStateOf<CustomerDataClient.Snapshot?>(null) }
    var customerSignInError by remember { mutableStateOf<String?>(null) }
    var customerSignInBusy by remember { mutableStateOf(false) }
    var customerInfoMessage by remember { mutableStateOf<String?>(null) }
    var customerMfaSetupSecret by remember { mutableStateOf<String?>(null) }
    var customerMfaChallenge by remember { mutableStateOf<CustomerAuthClient.MfaChallenge?>(null) }
    var pendingCustomerSession by remember { mutableStateOf<CustomerAuthClient.Session?>(null) }
    val customerAuthScope = rememberCoroutineScope()

    LaunchedEffect(backendUrl, activeCustomerToken, selectedAssetClass) {
        val token = activeCustomerToken?.takeIf { it.isNotBlank() }
        customerSnapshot = if (token == null || backendUrl.isBlank()) {
            null
        } else {
            runCatching {
                CustomerDataClient(backendUrl).loadSnapshot(
                    token,
                    scannerAssetClass = selectedAssetClass.scannerAssetClass
                )
            }.getOrNull()
        }
    }

    LaunchedEffect(backendUrl, activeCustomerToken, paperOrdersRefreshKey) {
        val token = activeCustomerToken?.takeIf { it.isNotBlank() }
        if (token == null || backendUrl.isBlank()) {
            paperOrders = emptyList()
            paperBalances = emptyList()
            paperOrdersBusy = false
            paperOrderError = null
        } else {
            paperOrdersBusy = true
            val client = CustomerDataClient(backendUrl)
            val balancesResult = runCatching { client.loadPaperBalances(token) }
            val ordersResult = runCatching { client.loadOpenPaperOrders(token) }
            if (activeCustomerToken == token) {
                balancesResult.onSuccess { paperBalances = it }
                ordersResult.onSuccess { paperOrders = it }
                val errors = listOfNotNull(
                    balancesResult.exceptionOrNull()?.message,
                    ordersResult.exceptionOrNull()?.message,
                )
                paperOrderError = errors.firstOrNull()
                paperOrdersBusy = false
            }
        }
    }

    LaunchedEffect(backendUrl, activeCustomerToken) {
        val token = activeCustomerToken?.takeIf { it.isNotBlank() }
        if (token == null || backendUrl.isBlank()) {
            selectedTimeframeDays = 30
            chartCrosshairEnabled = true
        } else {
            runCatching {
                CustomerDataClient(backendUrl).loadWorkspacePreferences(token)
            }.onSuccess { preferences ->
                selectedTimeframeDays = preferences.defaultChartWindowDays
                chartCrosshairEnabled = preferences.chartCrosshairEnabled
            }
        }
    }

    fun saveChartPreferences(
        defaultChartWindowDays: Int? = null,
        crosshairEnabled: Boolean? = null
    ) {
        val token = activeCustomerToken?.takeIf { it.isNotBlank() } ?: return
        if (backendUrl.isBlank()) return
        customerAuthScope.launch {
            runCatching {
                CustomerDataClient(backendUrl).updateWorkspacePreferences(
                    token,
                    defaultChartWindowDays = defaultChartWindowDays,
                    chartCrosshairEnabled = crosshairEnabled
                )
            }.onFailure {
                customerInfoMessage = "Could not sync chart preferences. Your current selection remains active on this screen."
            }
        }
    }

    // Only server-persisted, customer-scoped observations are eligible for the chart.
    // The API records at most one observation per 15-minute bucket when portfolio data is read.
    val equityHistory = remember(customerSnapshot?.equityHistory) {
        val observations = customerSnapshot?.equityHistory.orEmpty()
        val firstEquity = observations.firstOrNull()?.equity
        var previousEquity: Double? = null
        observations.mapIndexedNotNull { index, point ->
            val timestamp = runCatching {
                parseEquityObservationEpochMillis(point.observedAt)
            }.getOrNull() ?: return@mapIndexedNotNull null
            val dailyChange = previousEquity?.let { point.equity - it } ?: 0.0
            previousEquity = point.equity
            EquityPoint(
                day = index + 1,
                dateLabel = point.observedAt.take(16).replace('T', ' ') + " UTC",
                equity = point.equity,
                dailyPnl = dailyChange,
                cumProfit = point.equity - (firstEquity ?: point.equity),
                observedAtEpochMillis = timestamp
            )
        }
    }

    val displayPoints = remember(selectedTimeframeDays, equityHistory) {
        val cutoff = System.currentTimeMillis() -
            selectedTimeframeDays.toLong() * 24L * 60L * 60L * 1000L
        equityHistory.filter { it.observedAtEpochMillis >= cutoff }
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
                                userName = customerSnapshot?.profile?.displayName ?: userName,
                                brandTeal = brandTeal,
                                textPrimary = textPrimary,
                                onAvatarClick = { currentTab = DashboardTab.SETTINGS },
                                onNotificationClick = {
                                    actionDialogMessage = "Verified alerts are unavailable until an authenticated account and live alert feed are connected."
                                }
                            )
                        }

                        item {
                            if (activeCustomerToken.isNullOrBlank()) {
                                CustomerSignInCard(
                                    busy = customerSignInBusy,
                                    error = customerSignInError,
                                    infoMessage = customerInfoMessage,
                                    requiresAuthenticator = customerMfaChallenge != null,
                                    authenticatorSetupSecret = customerMfaSetupSecret,
                                    onSignIn = { email, password ->
                                        customerAuthScope.launch {
                                            customerSignInBusy = true
                                            customerSignInError = null
                                            customerInfoMessage = null
                                            try {
                                                val auth = CustomerAuthClient(backendUrl)
                                                val session = auth.login(email, password)
                                                val status = auth.mfaStatus(session.accessToken)
                                                val challenge = if (status.enabled && status.factorIds.isNotEmpty()) {
                                                    customerMfaSetupSecret = null
                                                    auth.challenge(session.accessToken, status.factorIds.first())
                                                } else {
                                                    val enrollment = auth.enroll(session.accessToken)
                                                    customerMfaSetupSecret = enrollment.secret
                                                    auth.challenge(session.accessToken, enrollment.factorId)
                                                }
                                                pendingCustomerSession = session
                                                customerMfaChallenge = challenge
                                            } catch (error: Exception) {
                                                pendingCustomerSession = null
                                                customerMfaChallenge = null
                                                customerMfaSetupSecret = null
                                                customerSignInError = error.message ?: "Customer sign-in failed"
                                            } finally {
                                                customerSignInBusy = false
                                            }
                                        }
                                    },
                                    onCreateAccount = { email, password ->
                                        customerAuthScope.launch {
                                            customerSignInBusy = true
                                            customerSignInError = null
                                            customerInfoMessage = null
                                            try {
                                                CustomerAuthClient(backendUrl).signUp(email, password)
                                                customerInfoMessage = "Registration submitted. Check your email and confirm the address before signing in."
                                            } catch (error: Exception) {
                                                customerSignInError = error.message ?: "Customer registration failed"
                                            } finally {
                                                customerSignInBusy = false
                                            }
                                        }
                                    },
                                    onVerifyAuthenticator = { code ->
                                        customerAuthScope.launch {
                                            customerSignInBusy = true
                                            customerSignInError = null
                                            try {
                                                val session = pendingCustomerSession
                                                    ?: throw IllegalStateException("Sign in again to request an authenticator challenge")
                                                val challenge = customerMfaChallenge
                                                    ?: throw IllegalStateException("Authenticator challenge expired; sign in again")
                                                val auth = CustomerAuthClient(backendUrl)
                                                val verified = auth.verify(session.accessToken, challenge, code)
                                                // TOTP has succeeded; clear the one-time challenge before
                                                // downstream operations can fail. A retry must return to
                                                // ordinary sign-in controls, not a consumed code-entry state.
                                                pendingCustomerSession = null
                                                customerMfaChallenge = null
                                                customerMfaSetupSecret = null
                                                auth.provisionProfile(verified.accessToken)
                                                auth.initializePaperAccount(verified.accessToken)
                                                activeCustomerToken = verified.accessToken
                                                customerInfoMessage = null
                                            } catch (error: Exception) {
                                                customerSignInError = error.message ?: "Authenticator verification failed"
                                            } finally {
                                                customerSignInBusy = false
                                            }
                                        }
                                    }
                                )
                            } else {
                                TextButton(onClick = {
                                    activeCustomerToken = null
                                    customerSnapshot = null
                                    customerSignInError = null
                                    customerInfoMessage = null
                                    customerMfaChallenge = null
                                    customerMfaSetupSecret = null
                                    pendingCustomerSession = null
                                }) {
                                    Text("Sign out customer")
                                }
                            }
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
                                totalBalance = customerSnapshot?.portfolio?.equity,
                                activeTrades = customerSnapshot?.portfolio?.openPositions,
                                profit24hPct = null,
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
                                onTimeframeChange = {
                                    selectedTimeframeDays = it
                                    saveChartPreferences(defaultChartWindowDays = it)
                                },
                                crosshairEnabled = chartCrosshairEnabled,
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
                                trades = customerSnapshot?.recentTrades.orEmpty(),
                                unavailableReason = customerSnapshot?.recentTradesUnavailableReason,
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
                                        "New Trade" -> {
                                            if (activeCustomerToken.isNullOrBlank()) {
                                                actionDialogMessage = "Sign in and verify your authenticator before opening the customer paper-order ticket."
                                            } else {
                                                paperOrderFeedback = null
                                                paperOrderError = null
                                                showPaperTradeDialog = true
                                            }
                                        }
                                        "Deposit" -> actionDialogMessage = "Customer funding is disabled in this build. No deposit address is provided here. Do not send funds based on this screen."
                                        "Withdraw" -> actionDialogMessage = "Customer withdrawals are disabled in this build. No payout will be submitted from this screen."
                                        "Analytics" -> currentTab = DashboardTab.PORTFOLIO
                                    }
                                }
                            )
                        }

                        // Watchlist Card (Multi-Asset: Crypto, Forex, Commodities)
                        item {
                            WatchlistCard(
                                selectedAssetClass = selectedAssetClass,
                                marketQuotes = customerSnapshot?.marketQuotes.orEmpty(),
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
                                onTimeframeChange = {
                                    selectedTimeframeDays = it
                                    saveChartPreferences(defaultChartWindowDays = it)
                                },
                                crosshairEnabled = chartCrosshairEnabled,
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
                                marketQuotes = customerSnapshot?.marketQuotes.orEmpty(),
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
                                    val scannerMessage = customerSnapshot?.marketQuotes
                                        ?.takeIf { it.isNotEmpty() }
                                        ?.joinToString("\n") {
                                            "${it.symbol}: ${it.price} • ${it.signal}"
                                        } ?: when (selectedAssetClass) {
                                            AssetClass.FOREX -> "FX quotes unavailable. Confirm the scanner entitlement and read-only provider response."
                                            AssetClass.COMMODITIES -> "Commodity quotes unavailable. This scanner requires configured OANDA practice credentials and supported instruments."
                                            else -> "Crypto market data unavailable. Sign in to an authorized customer account and confirm the market-scanner entitlement and provider."
                                        }
                                    Text(
                                        text = scannerMessage,
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
                        userName = customerSnapshot?.profile?.displayName ?: userName,
                        userEmail = customerSnapshot?.profile?.email ?: "Unavailable until account sign-in",
                        onOpenOperatorConsole = onOpenOperatorConsole,
                        onOpenPortal = onOpenPortal,
                        planName = customerSnapshot?.planName,
                        subscriptionStatus = customerSnapshot?.subscriptionStatus,
                        defaultChartWindowDays = selectedTimeframeDays,
                        onDefaultChartWindowChange = {
                            selectedTimeframeDays = it
                            saveChartPreferences(defaultChartWindowDays = it)
                        },
                        chartCrosshairEnabled = chartCrosshairEnabled,
                        onChartCrosshairChange = {
                            chartCrosshairEnabled = it
                            saveChartPreferences(crosshairEnabled = it)
                        },
                        onSignOut = {
                            // End the local session immediately. The remote revocation
                            // request is best-effort and must never keep the signed-in UI usable.
                            val token = activeCustomerToken
                            activeCustomerToken = null
                            customerSnapshot = null
                            customerMfaSetupSecret = null
                            customerMfaChallenge = null
                            pendingCustomerSession = null
                            customerInfoMessage = "Signed out on this device."
                            currentTab = DashboardTab.HOME

                            if (!token.isNullOrBlank() && backendUrl.isNotBlank()) {
                                customerAuthScope.launch {
                                    runCatching {
                                        CustomerAuthClient(backendUrl).logout(token)
                                    }.onFailure {
                                        // Do not overwrite the message or state of a new session
                                        // if the customer signs in while this request is in flight.
                                        if (activeCustomerToken == null) {
                                            customerInfoMessage =
                                                "Signed out on this device. Remote token revocation could not be confirmed."
                                        }
                                    }
                                }
                            }
                        },
                        modifier = Modifier.padding(innerPadding)
                    )
                }
            }
        }
    }

    if (showPaperTradeDialog) {
        PaperTradeDialog(
            orders = paperOrders,
            balances = paperBalances,
            ordersBusy = paperOrdersBusy,
            submittingOrUpdating = paperOrderBusy,
            feedback = paperOrderFeedback,
            error = paperOrderError,
            onDismiss = { showPaperTradeDialog = false },
            onRefresh = { paperOrdersRefreshKey += 1 },
            onSubmit = { symbol, side, quantity, orderType, limitPrice ->
                val token = activeCustomerToken?.takeIf { it.isNotBlank() }
                if (token == null || backendUrl.isBlank()) {
                    paperOrderError = "Customer sign-in is required to submit a paper order."
                } else {
                    paperOrderBusy = true
                    paperOrderError = null
                    paperOrderFeedback = null
                    customerAuthScope.launch {
                        runCatching {
                            CustomerDataClient(backendUrl).submitPaperOrder(
                                accessToken = token,
                                symbol = symbol,
                                side = side,
                                quantity = quantity,
                                orderType = orderType,
                                limitPrice = limitPrice,
                                timeframe = "1h"
                            )
                        }.onSuccess { action ->
                            paperOrderFeedback = "Paper order " + action.status.lowercase().replace('_', ' ') +
                                (action.order?.let { ". Current status: " + it.status } ?: "") +
                                (action.fillNote?.let { ". " + it } ?: "")
                            paperOrdersRefreshKey += 1
                        }.onFailure { error ->
                            paperOrderError = error.message ?: "Paper order submission failed"
                        }
                        paperOrderBusy = false
                    }
                }
            },
            onAdvance = { clientOrderId ->
                val token = activeCustomerToken?.takeIf { it.isNotBlank() }
                if (token != null && backendUrl.isNotBlank()) {
                    paperOrderBusy = true
                    paperOrderError = null
                    customerAuthScope.launch {
                        runCatching {
                            CustomerDataClient(backendUrl).advancePaperOrder(token, clientOrderId)
                        }.onSuccess { action ->
                            paperOrderFeedback = "Paper order " + action.status.lowercase().replace('_', ' ') +
                                (action.order?.let { ". Filled " + it.filledQuantity + " of " + it.quantity + "." } ?: "")
                            paperOrdersRefreshKey += 1
                        }.onFailure { error ->
                            paperOrderError = error.message ?: "Could not refresh paper order"
                        }
                        paperOrderBusy = false
                    }
                }
            },
            onCancel = { clientOrderId ->
                val token = activeCustomerToken?.takeIf { it.isNotBlank() }
                if (token != null && backendUrl.isNotBlank()) {
                    paperOrderBusy = true
                    paperOrderError = null
                    customerAuthScope.launch {
                        runCatching {
                            CustomerDataClient(backendUrl).cancelPaperOrder(token, clientOrderId)
                        }.onSuccess { action ->
                            paperOrderFeedback = "Paper order " + action.status.lowercase().replace('_', ' ') + "."
                            paperOrdersRefreshKey += 1
                        }.onFailure { error ->
                            paperOrderError = error.message ?: "Paper order cancellation failed"
                        }
                        paperOrderBusy = false
                    }
                }
            }
        )
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
private fun PaperTradeDialog(
    orders: List<CustomerDataClient.PaperOrder>,
    balances: List<CustomerDataClient.PaperBalance>,
    ordersBusy: Boolean,
    submittingOrUpdating: Boolean,
    feedback: String?,
    error: String?,
    onDismiss: () -> Unit,
    onRefresh: () -> Unit,
    onSubmit: (String, String, String, String, String?) -> Unit,
    onAdvance: (String) -> Unit,
    onCancel: (String) -> Unit
) {
    var symbol by remember { mutableStateOf("BTC/USDT") }
    var side by remember { mutableStateOf("BUY") }
    var orderType by remember { mutableStateOf("MARKET") }
    var quantity by remember { mutableStateOf("0.001") }
    var limitPrice by remember { mutableStateOf("") }
    val theme = com.atlas.trading.ui.theme.LocalAtlasColors.current
    val quantityValid = quantity.toBigDecimalOrNull()?.signum() == 1
    val limitValid = orderType != "LIMIT" || limitPrice.toBigDecimalOrNull()?.signum() == 1

    AlertDialog(
        onDismissRequest = onDismiss,
        title = {
            Column(verticalArrangement = Arrangement.spacedBy(4.dp)) {
                Text("New paper order", fontWeight = FontWeight.Bold, color = theme.textPrimary)
                Text("Virtual USDT ledger • Crypto spot only", fontSize = 12.sp, color = theme.textMuted)
            }
        },
        text = {
            Column(
                modifier = Modifier
                    .fillMaxWidth()
                    .heightIn(max = 520.dp)
                    .verticalScroll(rememberScrollState()),
                verticalArrangement = Arrangement.spacedBy(10.dp)
            ) {
                Text(
                    "This ticket submits only to Atlas's isolated customer paper ledger. It does not place a broker order, move real funds, or enable live trading. Paper fills require a later verified completed market bar.",
                    fontSize = 12.sp,
                    color = theme.textMuted
                )
                val virtualCash = balances.firstOrNull {
                    it.accountCode == "PAPER_CASH" && it.currency == "USDT"
                }?.balance
                Card(
                    modifier = Modifier.fillMaxWidth(),
                    colors = CardDefaults.cardColors(containerColor = theme.cardSurface),
                    border = CardDefaults.outlinedCardBorder().copy(brush = SolidColor(theme.cardBorder))
                ) {
                    Column(modifier = Modifier.padding(12.dp)) {
                        Text("Virtual cash balance", color = theme.textMuted, fontSize = 12.sp)
                        Text(
                            virtualCash?.let { "$it USDT" } ?: "Unavailable",
                            color = theme.textPrimary,
                            fontSize = 20.sp,
                            fontWeight = FontWeight.Bold
                        )
                    }
                }
                Text("Market", fontWeight = FontWeight.SemiBold, color = theme.textPrimary)
                Row(horizontalArrangement = Arrangement.spacedBy(8.dp)) {
                    listOf("BTC/USDT", "ETH/USDT").forEach { candidate ->
                        FilterChip(
                            selected = symbol == candidate,
                            onClick = { symbol = candidate },
                            label = { Text(candidate) },
                            enabled = !submittingOrUpdating
                        )
                    }
                }
                Text("Side", fontWeight = FontWeight.SemiBold, color = theme.textPrimary)
                Row(horizontalArrangement = Arrangement.spacedBy(8.dp)) {
                    FilterChip(
                        selected = side == "BUY",
                        onClick = { side = "BUY" },
                        label = { Text("Buy") },
                        enabled = !submittingOrUpdating
                    )
                    FilterChip(
                        selected = side == "SELL",
                        onClick = { side = "SELL" },
                        label = { Text("Sell") },
                        enabled = !submittingOrUpdating
                    )
                }
                Text("Order type", fontWeight = FontWeight.SemiBold, color = theme.textPrimary)
                Row(horizontalArrangement = Arrangement.spacedBy(8.dp)) {
                    FilterChip(
                        selected = orderType == "MARKET",
                        onClick = { orderType = "MARKET"; limitPrice = "" },
                        label = { Text("Market") },
                        enabled = !submittingOrUpdating
                    )
                    FilterChip(
                        selected = orderType == "LIMIT",
                        onClick = { orderType = "LIMIT" },
                        label = { Text("Limit") },
                        enabled = !submittingOrUpdating
                    )
                }
                OutlinedTextField(
                    value = quantity,
                    onValueChange = { quantity = it.filter { ch -> ch.isDigit() || ch == '.' }.take(24) },
                    modifier = Modifier.fillMaxWidth(),
                    label = { Text("Quantity in base asset") },
                    supportingText = { Text("Example: 0.001 BTC") },
                    singleLine = true,
                    enabled = !submittingOrUpdating,
                    keyboardOptions = KeyboardOptions(keyboardType = KeyboardType.Decimal)
                )
                if (orderType == "LIMIT") {
                    OutlinedTextField(
                        value = limitPrice,
                        onValueChange = { limitPrice = it.filter { ch -> ch.isDigit() || ch == '.' }.take(24) },
                        modifier = Modifier.fillMaxWidth(),
                        label = { Text("Limit price (USDT)") },
                        singleLine = true,
                        enabled = !submittingOrUpdating,
                        keyboardOptions = KeyboardOptions(keyboardType = KeyboardType.Decimal)
                    )
                }
                if (!feedback.isNullOrBlank()) {
                    Text(feedback, color = theme.emeraldAccent, fontSize = 12.sp)
                }
                if (!error.isNullOrBlank()) {
                    Text(error, color = theme.redDanger, fontSize = 12.sp)
                }
                Button(
                    onClick = {
                        onSubmit(symbol, side, quantity, orderType, limitPrice.takeIf { orderType == "LIMIT" })
                    },
                    modifier = Modifier.fillMaxWidth(),
                    enabled = !submittingOrUpdating && quantityValid && limitValid
                ) {
                    if (submittingOrUpdating) {
                        CircularProgressIndicator(modifier = Modifier.size(16.dp), strokeWidth = 2.dp)
                        Spacer(modifier = Modifier.width(8.dp))
                    }
                    Text(if (submittingOrUpdating) "Processing…" else "Submit virtual paper order")
                }
                Row(
                    modifier = Modifier.fillMaxWidth(),
                    horizontalArrangement = Arrangement.SpaceBetween,
                    verticalAlignment = Alignment.CenterVertically
                ) {
                    Text("Open paper orders", fontWeight = FontWeight.Bold, color = theme.textPrimary)
                    TextButton(onClick = onRefresh, enabled = !ordersBusy && !submittingOrUpdating) {
                        Text(if (ordersBusy) "Refreshing…" else "Refresh")
                    }
                }
                if (orders.isEmpty()) {
                    Text(
                        if (ordersBusy) "Loading open paper orders…" else "No open paper orders.",
                        color = theme.textMuted,
                        fontSize = 12.sp
                    )
                } else {
                    orders.forEach { order ->
                        Card(
                            modifier = Modifier.fillMaxWidth(),
                            colors = CardDefaults.cardColors(containerColor = theme.cardSurface),
                            border = CardDefaults.outlinedCardBorder().copy(brush = SolidColor(theme.cardBorder))
                        ) {
                            Column(
                                modifier = Modifier.padding(10.dp),
                                verticalArrangement = Arrangement.spacedBy(4.dp)
                            ) {
                                Text(
                                    "${order.side} ${order.quantity} ${order.symbol}",
                                    color = theme.textPrimary,
                                    fontWeight = FontWeight.SemiBold
                                )
                                Text(
                                    "${order.orderType} • ${order.status} • Filled ${order.filledQuantity}/${order.quantity}",
                                    color = theme.textMuted,
                                    fontSize = 11.sp
                                )
                                if (order.status in setOf("SUBMITTED", "PARTIALLY_FILLED")) {
                                    Row(horizontalArrangement = Arrangement.spacedBy(8.dp)) {
                                        TextButton(
                                            enabled = !submittingOrUpdating,
                                            onClick = { onAdvance(order.clientOrderId) }
                                        ) { Text("Check next bar") }
                                        TextButton(
                                            enabled = !submittingOrUpdating,
                                            onClick = { onCancel(order.clientOrderId) }
                                        ) { Text("Cancel order", color = theme.redDanger) }
                                    }
                                }
                            }
                        }
                    }
                }
            }
        },
        confirmButton = {
            TextButton(onClick = onDismiss) { Text("Done", color = theme.brandTeal) }
        },
        containerColor = theme.cardSurface
    )
}

@Composable
private fun CustomerSignInCard(
    busy: Boolean,
    error: String?,
    infoMessage: String?,
    requiresAuthenticator: Boolean,
    authenticatorSetupSecret: String?,
    onSignIn: (String, String) -> Unit,
    onCreateAccount: (String, String) -> Unit,
    onVerifyAuthenticator: (String) -> Unit
) {
    var email by remember { mutableStateOf("") }
    var password by remember { mutableStateOf("") }
    var authenticatorCode by remember { mutableStateOf("") }
    var creatingAccount by remember { mutableStateOf(false) }
    val theme = com.atlas.trading.ui.theme.LocalAtlasColors.current

    Card(
        modifier = Modifier.fillMaxWidth(),
        colors = CardDefaults.cardColors(containerColor = theme.cardSurface),
        shape = RoundedCornerShape(18.dp),
        border = CardDefaults.outlinedCardBorder().copy(brush = SolidColor(theme.cardBorder))
    ) {
        Column(
            modifier = Modifier.padding(16.dp),
            verticalArrangement = Arrangement.spacedBy(10.dp)
        ) {
            Text(
                if (requiresAuthenticator) "Secure your customer account"
                else if (creatingAccount) "Create a customer account"
                else "Sign in to your customer account",
                fontSize = 15.sp,
                fontWeight = FontWeight.SemiBold,
                color = theme.textPrimary
            )
            Text(
                "Paper trading uses a virtual ledger. Live trading and payouts are disabled in this staging app.",
                fontSize = 12.sp,
                color = theme.textMuted
            )
            OutlinedTextField(
                value = email,
                onValueChange = { email = it },
                modifier = Modifier.fillMaxWidth(),
                label = { Text("Email") },
                singleLine = true,
                enabled = !busy && !requiresAuthenticator
            )
            if (!requiresAuthenticator) {
                OutlinedTextField(
                    value = password,
                    onValueChange = { password = it },
                    modifier = Modifier.fillMaxWidth(),
                    label = { Text("Password") },
                    visualTransformation = androidx.compose.ui.text.input.PasswordVisualTransformation(),
                    singleLine = true,
                    enabled = !busy
                )
                Text(
                    if (creatingAccount) "We'll send an email confirmation before the account can be used."
                    else "After password sign-in, set up Google Authenticator if this account has no verified factor.",
                    fontSize = 12.sp,
                    color = theme.textMuted
                )
            } else {
                if (!authenticatorSetupSecret.isNullOrBlank()) {
                    Text(
                        "Add this setup key to Google Authenticator (or another TOTP app), then enter its current 6-digit code:",
                        fontSize = 12.sp,
                        color = theme.textMuted
                    )
                    Text(
                        authenticatorSetupSecret,
                        fontSize = 13.sp,
                        fontWeight = FontWeight.SemiBold,
                        color = theme.textPrimary
                    )
                } else {
                    Text(
                        "Enter the 6-digit code from your registered authenticator app.",
                        fontSize = 12.sp,
                        color = theme.textMuted
                    )
                }
                OutlinedTextField(
                    value = authenticatorCode,
                    onValueChange = { authenticatorCode = it.filter(Char::isDigit).take(6) },
                    modifier = Modifier.fillMaxWidth(),
                    label = { Text("Authenticator code") },
                    singleLine = true,
                    enabled = !busy
                )
            }
            if (!infoMessage.isNullOrBlank()) {
                Text(infoMessage, color = theme.brandTeal, fontSize = 12.sp)
            }
            if (!error.isNullOrBlank()) {
                Text(error, color = theme.redDanger, fontSize = 12.sp)
            }
            Button(
                onClick = {
                    when {
                        requiresAuthenticator -> onVerifyAuthenticator(authenticatorCode)
                        creatingAccount -> onCreateAccount(email, password)
                        else -> onSignIn(email, password)
                    }
                },
                enabled = !busy && if (requiresAuthenticator) {
                    authenticatorCode.length == 6
                } else {
                    email.isNotBlank() && (if (creatingAccount) password.length >= 8 else password.isNotBlank())
                },
                modifier = Modifier.fillMaxWidth()
            ) {
                if (busy) {
                    CircularProgressIndicator(modifier = Modifier.size(18.dp), strokeWidth = 2.dp)
                    Spacer(modifier = Modifier.width(8.dp))
                }
                Text(
                    when {
                        busy -> "Please wait…"
                        requiresAuthenticator -> "Verify authenticator"
                        creatingAccount -> "Create account"
                        else -> "Sign in securely"
                    }
                )
            }
            if (!requiresAuthenticator) {
                TextButton(
                    enabled = !busy,
                    onClick = { creatingAccount = !creatingAccount }
                ) {
                    Text(if (creatingAccount) "Already registered? Sign in" else "New customer? Create an account")
                }
            }
        }
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
    totalBalance: Double?,
    activeTrades: Int?,
    profit24hPct: Double?,
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
                Text(text = "Equity (USDT)", fontSize = 11.sp, color = textMuted, maxLines = 1)
                Spacer(modifier = Modifier.height(3.dp))
                Text(
                    text = totalBalance?.let { "USDT ${String.format(Locale.US, "%,.2f", it)}" } ?: "Unavailable",
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
                Text(text = "Open Positions", fontSize = 11.sp, color = textMuted, maxLines = 1)
                Spacer(modifier = Modifier.height(3.dp))
                Text(
                    text = activeTrades?.toString() ?: "Unavailable",
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
                        .background(if (profit24hPct != null) emeraldBadgeBg else cardBorder)
                        .padding(horizontal = 6.dp, vertical = 2.dp)
                ) {
                    Text(
                        text = profit24hPct?.let { "${String.format(Locale.US, "%.1f", it)}%" } ?: "Unavailable",
                        fontSize = 13.sp,
                        fontWeight = FontWeight.Bold,
                        color = if (profit24hPct != null) emeraldBadgeText else textMuted
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
    crosshairEnabled: Boolean = true,
    cardSurface: Color,
    cardBorder: Color,
    textPrimary: Color,
    textMuted: Color,
    emeraldAccent: Color
) {
    var selectedIndex by remember { mutableStateOf<Int?>(null) }
    LaunchedEffect(points) { selectedIndex = null }
    LaunchedEffect(crosshairEnabled) {
        if (!crosshairEnabled) selectedIndex = null
    }

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
                Text(
                    text = "Portfolio Equity (${selectedTimeframe}D)",
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
                            text = "${String.format(Locale.US, "%,.2f", activePoint.equity)} USDT",
                            fontSize = 20.sp,
                            fontWeight = FontWeight.Bold,
                            color = textPrimary
                        )
                        Text(
                            text = "${activePoint.dateLabel} • Change: ${String.format(Locale.US, "%+.2f", activePoint.cumProfit)} USDT",
                            fontSize = 11.sp,
                            color = emeraldAccent
                        )
                    }
                    Text(
                        text = if (selectedIndex != null) "Scrubbing active" else "Historical account data",
                        fontSize = 10.sp,
                        color = textMuted
                    )
                }
            }

            if (activePoint == null) {
                Text(
                    "Historical account performance unavailable — no verified customer history has been loaded.",
                    fontSize = 12.sp,
                    color = textMuted
                )
            }

            Spacer(modifier = Modifier.height(12.dp))

            // The Recharts-style Area Curve
            RechartsMonotoneAreaCanvas(
                points = points,
                selectedIndex = selectedIndex,
                onSelectIndex = { selectedIndex = it },
                crosshairEnabled = crosshairEnabled,
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
    crosshairEnabled: Boolean = true,
    accentColor: Color,
    modifier: Modifier = Modifier
) {
    if (points.isEmpty()) return

    val minEquity = points.minOf { it.equity } * 0.985
    val maxEquity = points.maxOf { it.equity } * 1.015
    val equityRange = max(1.0, maxEquity - minEquity)

    val interactionModifier = if (crosshairEnabled) {
        Modifier
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
    } else {
        Modifier
    }

    Canvas(modifier = modifier.then(interactionModifier)) {
        val width = size.width
        val height = size.height
        val n = points.size

        // A single persisted observation is still useful account history. Show its
        // exact sample as a point instead of leaving the chart area visually blank;
        // do not invent a line or interpolate missing observations.
        if (n == 1) {
            drawCircle(
                color = accentColor,
                radius = 6.dp.toPx(),
                center = Offset(width / 2f, height / 2f)
            )
            return@Canvas
        }
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
        val activeIdx = safeEquityChartSelectedIndex(selectedIndex, points.size) ?: return@Canvas
        val activeCoord = computedCoords[activeIdx]

        // Guide vertical line if scrubbing
        if (crosshairEnabled && selectedIndex != null) {
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
    trades: List<CustomerDataClient.RecentTrade>,
    unavailableReason: String?,
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
            Text("Recent customer trades", fontSize = 14.sp, fontWeight = FontWeight.SemiBold, color = textPrimary)
            Spacer(modifier = Modifier.height(8.dp))
            when {
                trades.isNotEmpty() -> {
                    trades.take(5).forEachIndexed { index, trade ->
                        if (index > 0) Spacer(modifier = Modifier.height(8.dp))
                        Row(
                            modifier = Modifier.fillMaxWidth(),
                            horizontalArrangement = Arrangement.SpaceBetween,
                            verticalAlignment = Alignment.CenterVertically
                        ) {
                            Column(modifier = Modifier.weight(1f)) {
                                Text(
                                    text = trade.symbol,
                                    fontSize = 13.sp,
                                    fontWeight = FontWeight.SemiBold,
                                    color = textPrimary,
                                    maxLines = 1
                                )
                                Text(
                                    text = "${trade.side} • ${trade.mode} • ${trade.filledQuantity} filled",
                                    fontSize = 10.sp,
                                    color = textMuted,
                                    maxLines = 1
                                )
                            }
                            Spacer(modifier = Modifier.width(8.dp))
                            Text(
                                text = trade.status.replace('_', ' '),
                                fontSize = 10.sp,
                                fontWeight = FontWeight.Bold,
                                color = when (trade.status) {
                                    "FILLED", "CLOSED", "SETTLED" -> emeraldAccent
                                    "REJECTED", "FAILED", "CANCELLED" -> redDanger
                                    else -> textMuted
                                },
                                maxLines = 1
                            )
                        }
                        if (trade.averageFillPrice != "0" && trade.averageFillPrice != "0.0") {
                            Text(
                                text = "Average fill: ${trade.averageFillPrice}",
                                fontSize = 10.sp,
                                color = textMuted
                            )
                        }
                    }
                }
                unavailableReason != null -> Text(
                    text = unavailableReason,
                    fontSize = 12.sp,
                    color = textMuted
                )
                else -> Text(
                    text = "No persisted trade activity yet. Completed or pending customer orders will appear here.",
                    fontSize = 12.sp,
                    color = textMuted
                )
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
    marketQuotes: List<CustomerDataClient.MarketQuote> = emptyList(),
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
            Text(
                "Watchlist (${selectedAssetClass.label})",
                fontSize = 14.sp,
                fontWeight = FontWeight.SemiBold,
                color = textPrimary
            )
            Spacer(modifier = Modifier.height(8.dp))
            when (selectedAssetClass) {
                AssetClass.FOREX -> {
                    if (marketQuotes.isEmpty()) {
                        Text(
                            "No verified FX quotes are available. The scanner uses read-only FX market data.",
                            fontSize = 12.sp,
                            color = textMuted
                        )
                    } else {
                        marketQuotes.forEach { quote ->
                            Text(
                                "${quote.symbol}  ${quote.price}  •  ${quote.signal}",
                                fontSize = 13.sp,
                                color = textPrimary,
                                modifier = Modifier.padding(vertical = 4.dp)
                            )
                        }
                    }
                }
                AssetClass.COMMODITIES -> {
                    if (marketQuotes.isEmpty()) {
                        Text(
                            "No verified commodity quotes are available from the configured OANDA practice feed.",
                            fontSize = 12.sp,
                            color = textMuted
                        )
                    } else {
                        marketQuotes.forEach { quote ->
                            Text(
                                "${quote.symbol}  ${quote.price}  •  ${quote.signal}",
                                fontSize = 13.sp,
                                color = textPrimary,
                                modifier = Modifier.padding(vertical = 4.dp)
                            )
                        }
                    }
                }
                else -> {
                    if (selectedAssetClass == AssetClass.ALL) {
                        Text(
                            "Showing the crypto feed. Select Forex or Commodities to load their read-only scanner data.",
                            fontSize = 10.sp,
                            color = textMuted
                        )
                    }
                    if (marketQuotes.isEmpty()) {
                        Text(
                            "Crypto prices unavailable — sign in to an authorized customer account and confirm market-scanner access. No placeholder prices are shown.",
                            fontSize = 12.sp,
                            color = textMuted
                        )
                    } else {
                        marketQuotes.forEach { quote ->
                            Text(
                                "${quote.symbol}  ${quote.price}  •  ${quote.signal}",
                                fontSize = 13.sp,
                                color = textPrimary,
                                modifier = Modifier.padding(vertical = 4.dp)
                            )
                        }
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
 * Synthetic equity history is intentionally never generated; charts use persisted customer observations.
 */
internal fun generate30DayEquityData(): List<EquityPoint> = emptyList()
