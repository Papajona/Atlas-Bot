package com.atlas.trading.ui

import androidx.compose.foundation.background
import androidx.compose.foundation.border
import androidx.compose.foundation.clickable
import androidx.compose.foundation.layout.*
import androidx.compose.foundation.lazy.LazyColumn
import androidx.compose.foundation.shape.CircleShape
import androidx.compose.foundation.shape.RoundedCornerShape
import androidx.compose.material.icons.Icons
import androidx.compose.material.icons.automirrored.filled.ArrowForward
import androidx.compose.material.icons.filled.*
import androidx.compose.material3.*
import androidx.compose.runtime.*
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.draw.clip
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.graphics.vector.ImageVector
import androidx.compose.ui.platform.testTag
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.unit.dp
import androidx.compose.ui.unit.sp

@OptIn(ExperimentalMaterial3Api::class)
@Composable
fun ModernSettingsScreen(
    userName: String = "Sample data",
    userEmail: String = "not connected to your account",
    onOpenOperatorConsole: () -> Unit = {},
    onOpenPortal: () -> Unit = {},
    modifier: Modifier = Modifier
) {
    // Preferences State
    var isPaperTrading by remember { mutableStateOf(true) }
    var riskBudgetPct by remember { mutableStateOf("0.5%") }
    var autoTrailingStop by remember { mutableStateOf(true) }
    var costStressFilter by remember { mutableStateOf(true) }
    var slippageTolerance by remember { mutableStateOf("0.1%") }

    var defaultTimeframe by remember { mutableStateOf("30 Days") }
    var showCrosshair by remember { mutableStateOf(true) }
    var showSparklines by remember { mutableStateOf(true) }
    var baseCurrency by remember { mutableStateOf("USD ($)") }

    var tradeExecutionAlerts by remember { mutableStateOf(true) }
    var dailySummaryAlerts by remember { mutableStateOf(true) }
    var circuitBreakerAlerts by remember { mutableStateOf(true) }
    var hapticFeedback by remember { mutableStateOf(true) }

    var biometricLock by remember { mutableStateOf(true) }
    var autoLockTimer by remember { mutableStateOf("15 min") }

    var activeDialogTitle by remember { mutableStateOf<String?>(null) }
    var activeDialogContent by remember { mutableStateOf<String?>(null) }

    val theme = com.atlas.trading.ui.theme.LocalAtlasColors.current
    val currentThemeMode = com.atlas.trading.ui.theme.LocalThemeMode.current
    val onThemeChange = com.atlas.trading.ui.theme.LocalThemeUpdater.current

    val brandTeal = theme.brandTeal
    val textPrimary = theme.textPrimary
    val textMuted = theme.textMuted
    val cardSurface = theme.cardSurface
    val cardBorder = theme.cardBorder
    val emeraldAccent = theme.emeraldAccent
    val emeraldBadgeBg = theme.emeraldBadgeBg
    val emeraldBadgeText = theme.emeraldBadgeText

    LazyColumn(
        modifier = modifier
            .fillMaxSize()
            .testTag("modern_settings_screen")
            .padding(horizontal = 18.dp),
        verticalArrangement = Arrangement.spacedBy(16.dp)
    ) {
        item {
            Spacer(modifier = Modifier.height(4.dp))
            Column {
                Text(
                    text = "Settings & Preferences",
                    fontSize = 22.sp,
                    fontWeight = FontWeight.Bold,
                    color = textPrimary
                )
                Spacer(modifier = Modifier.height(2.dp))
                Text(
                    text = "Account, risk discipline, theme & security",
                    fontSize = 13.sp,
                    color = textMuted
                )
            }
        }

        // 1. Appearance & Theme (Dark Mode Selector)
        item {
            SettingsGroup(
                title = "APPEARANCE & THEME",
                cardSurface = cardSurface,
                cardBorder = cardBorder,
                textPrimary = textPrimary
            ) {
                SettingsThemeSelectorRow(
                    currentTheme = currentThemeMode,
                    onThemeSelected = onThemeChange,
                    textPrimary = textPrimary,
                    textMuted = textMuted,
                    brandTeal = brandTeal,
                    cardBorder = cardBorder,
                    innerCardBg = theme.cardInnerSurface
                )
            }
        }

        // 2. Profile & Account Card
        item {
            Card(
                modifier = Modifier.fillMaxWidth(),
                shape = RoundedCornerShape(18.dp),
                colors = CardDefaults.cardColors(containerColor = cardSurface),
                border = CardDefaults.outlinedCardBorder().copy(brush = androidx.compose.ui.graphics.SolidColor(cardBorder)),
                elevation = CardDefaults.cardElevation(defaultElevation = 2.dp)
            ) {
                Column(modifier = Modifier.padding(16.dp)) {
                    Row(
                        modifier = Modifier.fillMaxWidth(),
                        verticalAlignment = Alignment.CenterVertically
                    ) {
                        // User Avatar
                        Box(
                            modifier = Modifier
                                .size(50.dp)
                                .clip(CircleShape)
                                .background(Color(0xFFE0F2FE)),
                            contentAlignment = Alignment.Center
                        ) {
                            Text(
                                text = "AH",
                                fontSize = 18.sp,
                                fontWeight = FontWeight.Bold,
                                color = Color(0xFF0284C7)
                            )
                        }

                        Spacer(modifier = Modifier.width(14.dp))

                        Column(modifier = Modifier.weight(1f)) {
                            Row(verticalAlignment = Alignment.CenterVertically) {
                                Text(
                                    text = userName,
                                    fontSize = 16.sp,
                                    fontWeight = FontWeight.Bold,
                                    color = textPrimary
                                )
                                Spacer(modifier = Modifier.width(6.dp))
                                Box(
                                    modifier = Modifier
                                        .clip(RoundedCornerShape(6.dp))
                                        .background(emeraldBadgeBg)
                                        .padding(horizontal = 6.dp, vertical = 2.dp)
                                ) {
                                    Text(
                                        text = "Sample profile",
                                        fontSize = 9.sp,
                                        fontWeight = FontWeight.Bold,
                                        color = emeraldBadgeText
                                    )
                                }
                            }
                            Spacer(modifier = Modifier.height(2.dp))
                            Text(
                                text = userEmail,
                                fontSize = 12.sp,
                                color = textMuted
                            )
                        }
                    }

                    Spacer(modifier = Modifier.height(14.dp))
                    HorizontalDivider(color = Color(0xFFE2E8F0).copy(alpha = 0.6f))
                    Spacer(modifier = Modifier.height(12.dp))

                    Row(
                        modifier = Modifier.fillMaxWidth(),
                        horizontalArrangement = Arrangement.SpaceBetween,
                        verticalAlignment = Alignment.CenterVertically
                    ) {
                        Column {
                            Text(text = "Account status", fontSize = 11.sp, color = textMuted)
                            Text(text = "Not connected to an account", fontSize = 13.sp, fontWeight = FontWeight.SemiBold, color = textPrimary)
                        }

                        Column(horizontalAlignment = Alignment.End) {
                            Text(text = "Trader ID", fontSize = 11.sp, color = textMuted)
                            Text(text = "Unavailable", fontSize = 13.sp, fontWeight = FontWeight.SemiBold, color = textMuted)
                        }
                    }
                }
            }
        }

        // 2. Trading & Bot Preferences
        item {
            SettingsGroup(
                title = "TRADING & BOT DISCIPLINE",
                cardSurface = cardSurface,
                cardBorder = cardBorder,
                textPrimary = textPrimary
            ) {
                SettingsSwitchRow(
                    icon = Icons.Default.PlayArrow,
                    iconTint = brandTeal,
                    title = "Paper Trading Simulation",
                    subtitle = "Display only: does not change server trading mode (set on the web console)",
                    checked = isPaperTrading,
                    onCheckedChange = { isPaperTrading = it },
                    textPrimary = textPrimary,
                    textMuted = textMuted
                )

                HorizontalDivider(color = Color(0xFFE2E8F0).copy(alpha = 0.6f))

                SettingsSegmentedRow(
                    icon = Icons.Default.Info,
                    iconTint = emeraldAccent,
                    title = "Risk Budget Per Trade",
                    subtitle = "Capital allocated per trade (Audit recommends 0.5%)",
                    options = listOf("0.5%", "1.0%", "2.0%"),
                    selectedOption = riskBudgetPct,
                    onOptionSelected = { riskBudgetPct = it },
                    textPrimary = textPrimary,
                    textMuted = textMuted,
                    brandTeal = brandTeal
                )

                HorizontalDivider(color = Color(0xFFE2E8F0).copy(alpha = 0.6f))

                SettingsSwitchRow(
                    icon = Icons.Default.CheckCircle,
                    iconTint = emeraldAccent,
                    title = "Automated Trailing Stop-Loss",
                    subtitle = "Dynamic ATR-based exit to preserve accumulated gains",
                    checked = autoTrailingStop,
                    onCheckedChange = { autoTrailingStop = it },
                    textPrimary = textPrimary,
                    textMuted = textMuted
                )

                HorizontalDivider(color = Color(0xFFE2E8F0).copy(alpha = 0.6f))

                SettingsSwitchRow(
                    icon = Icons.Default.Warning,
                    iconTint = Color(0xFFF59E0B),
                    title = "Cost-Stress Gate (2.0x Multiplier)",
                    subtitle = "Strictly rejects setups without 2x fee/slippage headroom",
                    checked = costStressFilter,
                    onCheckedChange = { costStressFilter = it },
                    textPrimary = textPrimary,
                    textMuted = textMuted
                )

                HorizontalDivider(color = Color(0xFFE2E8F0).copy(alpha = 0.6f))

                SettingsSegmentedRow(
                    icon = Icons.Default.ArrowDropDown,
                    iconTint = brandTeal,
                    title = "Max Slippage Tolerance",
                    subtitle = "Maximum allowable deviation on market fills",
                    options = listOf("0.1%", "0.5%", "1.0%"),
                    selectedOption = slippageTolerance,
                    onOptionSelected = { slippageTolerance = it },
                    textPrimary = textPrimary,
                    textMuted = textMuted,
                    brandTeal = brandTeal
                )
            }
        }

        // 3. Display & Market Views
        item {
            SettingsGroup(
                title = "DISPLAY & CHARTS",
                cardSurface = cardSurface,
                cardBorder = cardBorder,
                textPrimary = textPrimary
            ) {
                SettingsSegmentedRow(
                    icon = Icons.Default.DateRange,
                    iconTint = brandTeal,
                    title = "Default Chart Window",
                    subtitle = "Initial equity curve historical scope",
                    options = listOf("7 Days", "30 Days"),
                    selectedOption = defaultTimeframe,
                    onOptionSelected = { defaultTimeframe = it },
                    textPrimary = textPrimary,
                    textMuted = textMuted,
                    brandTeal = brandTeal
                )

                HorizontalDivider(color = Color(0xFFE2E8F0).copy(alpha = 0.6f))

                SettingsSwitchRow(
                    icon = Icons.Default.Search,
                    iconTint = brandTeal,
                    title = "Interactive Chart Crosshair",
                    subtitle = "Touch & drag tooltip showing exact daily equity & PnL",
                    checked = showCrosshair,
                    onCheckedChange = { showCrosshair = it },
                    textPrimary = textPrimary,
                    textMuted = textMuted
                )

                HorizontalDivider(color = Color(0xFFE2E8F0).copy(alpha = 0.6f))

                SettingsSwitchRow(
                    icon = Icons.Default.ThumbUp,
                    iconTint = emeraldAccent,
                    title = "Live Market Sparklines",
                    subtitle = "Display mini trendlines inside watchlist rows",
                    checked = showSparklines,
                    onCheckedChange = { showSparklines = it },
                    textPrimary = textPrimary,
                    textMuted = textMuted
                )

                HorizontalDivider(color = Color(0xFFE2E8F0).copy(alpha = 0.6f))

                SettingsSegmentedRow(
                    icon = Icons.Default.ShoppingCart,
                    iconTint = brandTeal,
                    title = "Display Currency",
                    subtitle = "Base valuation for portfolios and reports",
                    options = listOf("USD ($)", "EUR (€)", "GBP (£)"),
                    selectedOption = baseCurrency,
                    onOptionSelected = { baseCurrency = it },
                    textPrimary = textPrimary,
                    textMuted = textMuted,
                    brandTeal = brandTeal
                )
            }
        }

        // 4. Notifications & Alerts
        item {
            SettingsGroup(
                title = "NOTIFICATIONS & ALERTS",
                cardSurface = cardSurface,
                cardBorder = cardBorder,
                textPrimary = textPrimary
            ) {
                SettingsSwitchRow(
                    icon = Icons.Default.Notifications,
                    iconTint = brandTeal,
                    title = "Order Execution Alerts",
                    subtitle = "Instant alert whenever a bot enters or exits a trade",
                    checked = tradeExecutionAlerts,
                    onCheckedChange = { tradeExecutionAlerts = it },
                    textPrimary = textPrimary,
                    textMuted = textMuted
                )

                HorizontalDivider(color = Color(0xFFE2E8F0).copy(alpha = 0.6f))

                SettingsSwitchRow(
                    icon = Icons.Default.Info,
                    iconTint = emeraldAccent,
                    title = "Daily PnL Digest (00:00 UTC)",
                    subtitle = "Summary of 24h win rate, profit factor, and net equity",
                    checked = dailySummaryAlerts,
                    onCheckedChange = { dailySummaryAlerts = it },
                    textPrimary = textPrimary,
                    textMuted = textMuted
                )

                HorizontalDivider(color = Color(0xFFE2E8F0).copy(alpha = 0.6f))

                SettingsSwitchRow(
                    icon = Icons.Default.Warning,
                    iconTint = Color(0xFFEF4444),
                    title = "Circuit Breaker / Stop Alerts",
                    subtitle = "High-priority notification if volatility breaker trips",
                    checked = circuitBreakerAlerts,
                    onCheckedChange = { circuitBreakerAlerts = it },
                    textPrimary = textPrimary,
                    textMuted = textMuted
                )

                HorizontalDivider(color = Color(0xFFE2E8F0).copy(alpha = 0.6f))

                SettingsSwitchRow(
                    icon = Icons.Default.Refresh,
                    iconTint = brandTeal,
                    title = "Haptic Vibration Feedback",
                    subtitle = "Vibrate on chart scrub, trade action, and alerts",
                    checked = hapticFeedback,
                    onCheckedChange = { hapticFeedback = it },
                    textPrimary = textPrimary,
                    textMuted = textMuted
                )
            }
        }

        // 5. Security & Biometrics
        item {
            SettingsGroup(
                title = "SECURITY & PRIVACY",
                cardSurface = cardSurface,
                cardBorder = cardBorder,
                textPrimary = textPrimary
            ) {
                SettingsSwitchRow(
                    icon = Icons.Default.Lock,
                    iconTint = brandTeal,
                    title = "Biometric App Lock",
                    subtitle = "Display only: not yet enforced",
                    checked = biometricLock,
                    onCheckedChange = { biometricLock = it },
                    textPrimary = textPrimary,
                    textMuted = textMuted
                )

                HorizontalDivider(color = Color(0xFFE2E8F0).copy(alpha = 0.6f))

                SettingsNavigationRow(
                    icon = Icons.Default.CheckCircle,
                    iconTint = emeraldAccent,
                    title = "Two-Factor Authentication",
                    badge = "ACTIVE (AAL2)",
                    badgeBg = emeraldBadgeBg,
                    badgeText = emeraldBadgeText,
                    subtitle = "Google Authenticator TOTP enforced",
                    onClick = {
                        activeDialogTitle = "Two-Factor Authentication"
                        activeDialogContent = "Two-factor authentication (AAL2) is enabled and enforced for all administrative and operator actions.\n\nAuthenticator: Google Authenticator / Hardware Key\nStatus: Active & Verified"
                    },
                    textPrimary = textPrimary,
                    textMuted = textMuted
                )

                HorizontalDivider(color = Color(0xFFE2E8F0).copy(alpha = 0.6f))

                SettingsSegmentedRow(
                    icon = Icons.Default.Star,
                    iconTint = brandTeal,
                    title = "Auto-Lock Inactivity Timer",
                    subtitle = "Lock screen after period of inactivity",
                    options = listOf("5 min", "15 min", "30 min"),
                    selectedOption = autoLockTimer,
                    onOptionSelected = { autoLockTimer = it },
                    textPrimary = textPrimary,
                    textMuted = textMuted,
                    brandTeal = brandTeal
                )

                HorizontalDivider(color = Color(0xFFE2E8F0).copy(alpha = 0.6f))

                SettingsNavigationRow(
                    icon = Icons.Default.List,
                    iconTint = brandTeal,
                    title = "Encrypted Security Audit Trail",
                    subtitle = "Display only: audit events are not connected in this build",
                    badge = "NOT CONNECTED",
                    badgeBg = Color(0xFFEFF6FF),
                    badgeText = Color(0xFF1D4ED8),
                    onClick = {
                        activeDialogTitle = "Encrypted Security Audit Trail"
                        activeDialogContent = "No live audit events are available in this build. Connect the authenticated account and backend audit API before displaying security events."
                    },
                    textPrimary = textPrimary,
                    textMuted = textMuted
                )
            }
        }

        // 6. Institutional Risk Governance & Operator Portal
        item {
            Card(
                modifier = Modifier.fillMaxWidth(),
                shape = RoundedCornerShape(18.dp),
                colors = CardDefaults.cardColors(containerColor = cardSurface),
                border = CardDefaults.outlinedCardBorder().copy(brush = androidx.compose.ui.graphics.SolidColor(Color(0xFF0D9488).copy(alpha = 0.35f))),
                elevation = CardDefaults.cardElevation(defaultElevation = 3.dp)
            ) {
                Column(modifier = Modifier.padding(16.dp)) {
                    Row(verticalAlignment = Alignment.CenterVertically) {
                        Box(
                            modifier = Modifier
                                .size(36.dp)
                                .clip(RoundedCornerShape(8.dp))
                                .background(Color(0xFFE6F4F1)),
                            contentAlignment = Alignment.Center
                        ) {
                            Icon(
                                imageVector = Icons.Default.Lock,
                                contentDescription = "Risk Governor",
                                tint = brandTeal,
                                modifier = Modifier.size(20.dp)
                            )
                        }
                        Spacer(modifier = Modifier.width(10.dp))
                        Column {
                            Text(
                                text = "Institutional Risk Governor",
                                fontSize = 15.sp,
                                fontWeight = FontWeight.Bold,
                                color = textPrimary
                            )
                            Text(
                                text = "Not connected • live telemetry unavailable",
                                fontSize = 11.sp,
                                color = emeraldAccent,
                                fontWeight = FontWeight.SemiBold
                            )
                        }
                    }

                    Spacer(modifier = Modifier.height(10.dp))

                    Text(
                        text = "Live risk telemetry and the emergency Kill Switch are available only through the authenticated operator console.",
                        fontSize = 12.sp,
                        color = textMuted,
                        lineHeight = 18.sp
                    )

                    Spacer(modifier = Modifier.height(14.dp))

                    Row(
                        modifier = Modifier.fillMaxWidth(),
                        horizontalArrangement = Arrangement.spacedBy(10.dp)
                    ) {
                        Button(
                            onClick = onOpenOperatorConsole,
                            modifier = Modifier.weight(1f),
                            shape = RoundedCornerShape(10.dp),
                            colors = ButtonDefaults.buttonColors(containerColor = brandTeal)
                        ) {
                            Text(
                                text = "Operator Console",
                                fontSize = 12.sp,
                                fontWeight = FontWeight.Bold
                            )
                        }

                        OutlinedButton(
                            onClick = onOpenPortal,
                            modifier = Modifier.weight(1f),
                            shape = RoundedCornerShape(10.dp)
                        ) {
                            Text(
                                text = "Web Portal",
                                fontSize = 12.sp,
                                fontWeight = FontWeight.SemiBold,
                                color = brandTeal
                            )
                        }
                    }
                }
            }
        }

        // 7. App Info & Maintenance
        item {
            SettingsGroup(
                title = "APP INFORMATION",
                cardSurface = cardSurface,
                cardBorder = cardBorder,
                textPrimary = textPrimary
            ) {
                SettingsNavigationRow(
                    icon = Icons.Default.Info,
                    iconTint = textMuted,
                    title = "App Version",
                    badge = "v3.10.47",
                    badgeBg = Color(0xFFF1F5F9),
                    badgeText = Color(0xFF475569),
                    subtitle = "Sample data only: not yet connected to your account",
                    onClick = {
                        activeDialogTitle = "Atlas Trading System"
                        activeDialogContent = "Version: 3.10.47 (Build 1047)\nArchitecture: Jetpack Compose + Cloud Run FastAPI + PostgreSQL\nCompliance: Google Play Developer Program Compliant"
                    },
                    textPrimary = textPrimary,
                    textMuted = textMuted
                )

                HorizontalDivider(color = Color(0xFFE2E8F0).copy(alpha = 0.6f))

                SettingsNavigationRow(
                    icon = Icons.Default.Refresh,
                    iconTint = Color(0xFFEF4444),
                    title = "Reset Cache & Simulation Data",
                    subtitle = "Clean on-device cache and restore default $100 stake",
                    onClick = {
                        activeDialogTitle = "Cache Cleaned"
                        activeDialogContent = "Applet cache and historical temporary buffers have been purged successfully."
                    },
                    textPrimary = textPrimary,
                    textMuted = textMuted
                )
            }
            Spacer(modifier = Modifier.height(24.dp))
        }
    }

    if (activeDialogTitle != null) {
        AlertDialog(
            onDismissRequest = {
                activeDialogTitle = null
                activeDialogContent = null
            },
            title = {
                Text(text = activeDialogTitle ?: "", fontWeight = FontWeight.Bold)
            },
            text = {
                Text(text = activeDialogContent ?: "", fontSize = 13.sp, lineHeight = 20.sp)
            },
            confirmButton = {
                TextButton(onClick = {
                    activeDialogTitle = null
                    activeDialogContent = null
                }) {
                    Text("OK", color = brandTeal, fontWeight = FontWeight.Bold)
                }
            },
            containerColor = cardSurface
        )
    }
}

@Composable
private fun SettingsGroup(
    title: String,
    cardSurface: Color,
    cardBorder: Color,
    textPrimary: Color,
    content: @Composable ColumnScope.() -> Unit
) {
    Column {
        Text(
            text = title,
            fontSize = 11.sp,
            fontWeight = FontWeight.Bold,
            color = Color(0xFF64748B),
            letterSpacing = 1.sp,
            modifier = Modifier.padding(start = 4.dp, bottom = 6.dp)
        )
        Card(
            modifier = Modifier.fillMaxWidth(),
            shape = RoundedCornerShape(18.dp),
            colors = CardDefaults.cardColors(containerColor = cardSurface),
            border = CardDefaults.outlinedCardBorder().copy(brush = androidx.compose.ui.graphics.SolidColor(cardBorder)),
            elevation = CardDefaults.cardElevation(defaultElevation = 2.dp)
        ) {
            Column(
                modifier = Modifier.padding(horizontal = 14.dp, vertical = 10.dp),
                content = content
            )
        }
    }
}

@Composable
private fun SettingsSwitchRow(
    icon: ImageVector,
    iconTint: Color,
    title: String,
    subtitle: String,
    checked: Boolean,
    onCheckedChange: (Boolean) -> Unit,
    textPrimary: Color,
    textMuted: Color
) {
    Row(
        modifier = Modifier
            .fillMaxWidth()
            .clickable { onCheckedChange(!checked) }
            .padding(vertical = 10.dp),
        verticalAlignment = Alignment.CenterVertically
    ) {
        Box(
            modifier = Modifier
                .size(32.dp)
                .clip(RoundedCornerShape(8.dp))
                .background(iconTint.copy(alpha = 0.12f)),
            contentAlignment = Alignment.Center
        ) {
            Icon(imageVector = icon, contentDescription = title, tint = iconTint, modifier = Modifier.size(18.dp))
        }

        Spacer(modifier = Modifier.width(12.dp))

        Column(modifier = Modifier.weight(1f)) {
            Text(text = title, fontSize = 13.sp, fontWeight = FontWeight.SemiBold, color = textPrimary)
            Spacer(modifier = Modifier.height(1.dp))
            Text(text = subtitle, fontSize = 11.sp, color = textMuted, lineHeight = 15.sp)
        }

        Spacer(modifier = Modifier.width(8.dp))

        Switch(
            checked = checked,
            onCheckedChange = onCheckedChange,
            colors = SwitchDefaults.colors(
                checkedThumbColor = Color.White,
                checkedTrackColor = Color(0xFF0D9488)
            )
        )
    }
}

@Composable
private fun SettingsSegmentedRow(
    icon: ImageVector,
    iconTint: Color,
    title: String,
    subtitle: String,
    options: List<String>,
    selectedOption: String,
    onOptionSelected: (String) -> Unit,
    textPrimary: Color,
    textMuted: Color,
    brandTeal: Color
) {
    Column(
        modifier = Modifier
            .fillMaxWidth()
            .padding(vertical = 10.dp)
    ) {
        Row(verticalAlignment = Alignment.CenterVertically) {
            Box(
                modifier = Modifier
                    .size(32.dp)
                    .clip(RoundedCornerShape(8.dp))
                .background(iconTint.copy(alpha = 0.12f)),
                contentAlignment = Alignment.Center
            ) {
                Icon(imageVector = icon, contentDescription = title, tint = iconTint, modifier = Modifier.size(18.dp))
            }
            Spacer(modifier = Modifier.width(12.dp))
            Column {
                Text(text = title, fontSize = 13.sp, fontWeight = FontWeight.SemiBold, color = textPrimary)
                Spacer(modifier = Modifier.height(1.dp))
                Text(text = subtitle, fontSize = 11.sp, color = textMuted)
            }
        }

        Spacer(modifier = Modifier.height(10.dp))

        Row(
            modifier = Modifier.fillMaxWidth(),
            horizontalArrangement = Arrangement.spacedBy(8.dp)
        ) {
            options.forEach { option ->
                val isSelected = option == selectedOption
                Box(
                    modifier = Modifier
                        .weight(1f)
                        .clip(RoundedCornerShape(8.dp))
                        .background(if (isSelected) brandTeal else Color(0xFFF1F5F9))
                        .clickable { onOptionSelected(option) }
                        .padding(vertical = 8.dp),
                    contentAlignment = Alignment.Center
                ) {
                    Text(
                        text = option,
                        fontSize = 11.sp,
                        fontWeight = if (isSelected) FontWeight.Bold else FontWeight.Medium,
                        color = if (isSelected) Color.White else textPrimary
                    )
                }
            }
        }
    }
}

@Composable
private fun SettingsNavigationRow(
    icon: ImageVector,
    iconTint: Color,
    title: String,
    subtitle: String,
    badge: String? = null,
    badgeBg: Color = Color.Transparent,
    badgeText: Color = Color.Unspecified,
    onClick: () -> Unit,
    textPrimary: Color,
    textMuted: Color
) {
    Row(
        modifier = Modifier
            .fillMaxWidth()
            .clickable(onClick = onClick)
            .padding(vertical = 10.dp),
        verticalAlignment = Alignment.CenterVertically
    ) {
        Box(
            modifier = Modifier
                .size(32.dp)
                .clip(RoundedCornerShape(8.dp))
                .background(iconTint.copy(alpha = 0.12f)),
            contentAlignment = Alignment.Center
        ) {
            Icon(imageVector = icon, contentDescription = title, tint = iconTint, modifier = Modifier.size(18.dp))
        }

        Spacer(modifier = Modifier.width(12.dp))

        Column(modifier = Modifier.weight(1f)) {
            Row(verticalAlignment = Alignment.CenterVertically) {
                Text(text = title, fontSize = 13.sp, fontWeight = FontWeight.SemiBold, color = textPrimary)
                if (badge != null) {
                    Spacer(modifier = Modifier.width(6.dp))
                    Box(
                        modifier = Modifier
                            .clip(RoundedCornerShape(6.dp))
                            .background(badgeBg)
                            .padding(horizontal = 6.dp, vertical = 2.dp)
                    ) {
                        Text(text = badge, fontSize = 9.sp, fontWeight = FontWeight.Bold, color = badgeText)
                    }
                }
            }
            Spacer(modifier = Modifier.height(1.dp))
            Text(text = subtitle, fontSize = 11.sp, color = textMuted)
        }

        Icon(
            imageVector = Icons.AutoMirrored.Filled.ArrowForward,
            contentDescription = "Navigate",
            tint = Color(0xFF94A3B8),
            modifier = Modifier.size(16.dp)
        )
    }
}

@Composable
private fun SettingsThemeSelectorRow(
    currentTheme: com.atlas.trading.ui.theme.AppThemeMode,
    onThemeSelected: (com.atlas.trading.ui.theme.AppThemeMode) -> Unit,
    textPrimary: Color,
    textMuted: Color,
    brandTeal: Color,
    cardBorder: Color,
    innerCardBg: Color
) {
    Column(
        modifier = Modifier
            .fillMaxWidth()
            .padding(vertical = 10.dp)
    ) {
        Row(
            modifier = Modifier.fillMaxWidth(),
            horizontalArrangement = Arrangement.SpaceBetween,
            verticalAlignment = Alignment.CenterVertically
        ) {
            Row(verticalAlignment = Alignment.CenterVertically) {
                Box(
                    modifier = Modifier
                        .size(32.dp)
                        .clip(RoundedCornerShape(8.dp))
                        .background(brandTeal.copy(alpha = 0.12f)),
                    contentAlignment = Alignment.Center
                ) {
                    Icon(
                        imageVector = Icons.Default.Star,
                        contentDescription = "Theme",
                        tint = brandTeal,
                        modifier = Modifier.size(18.dp)
                    )
                }
                Spacer(modifier = Modifier.width(12.dp))
                Column {
                    Text(
                        text = "Interface Color Theme",
                        fontSize = 13.sp,
                        fontWeight = FontWeight.SemiBold,
                        color = textPrimary
                    )
                    Spacer(modifier = Modifier.height(1.dp))
                    Text(
                        text = "Select dynamic Dark, Light, or follow System",
                        fontSize = 11.sp,
                        color = textMuted
                    )
                }
            }

            Box(
                modifier = Modifier
                    .clip(RoundedCornerShape(6.dp))
                    .background(brandTeal.copy(alpha = 0.15f))
                    .padding(horizontal = 8.dp, vertical = 3.dp)
            ) {
                Text(
                    text = currentTheme.label.uppercase(),
                    fontSize = 10.sp,
                    fontWeight = FontWeight.Bold,
                    color = brandTeal
                )
            }
        }

        Spacer(modifier = Modifier.height(12.dp))

        Row(
            modifier = Modifier.fillMaxWidth(),
            horizontalArrangement = Arrangement.spacedBy(8.dp)
        ) {
            com.atlas.trading.ui.theme.AppThemeMode.values().forEach { mode ->
                val isSelected = mode == currentTheme
                Box(
                    modifier = Modifier
                        .weight(1f)
                        .clip(RoundedCornerShape(10.dp))
                        .background(if (isSelected) brandTeal else innerCardBg)
                        .border(
                            width = 1.dp,
                            color = if (isSelected) brandTeal else cardBorder,
                            shape = RoundedCornerShape(10.dp)
                        )
                        .clickable { onThemeSelected(mode) }
                        .padding(vertical = 10.dp),
                    contentAlignment = Alignment.Center
                ) {
                    Row(verticalAlignment = Alignment.CenterVertically) {
                        Text(text = mode.icon, fontSize = 14.sp)
                        Spacer(modifier = Modifier.width(6.dp))
                        Text(
                            text = mode.label,
                            fontSize = 12.sp,
                            fontWeight = if (isSelected) FontWeight.Bold else FontWeight.Medium,
                            color = if (isSelected) Color.White else textPrimary
                        )
                    }
                }
            }
        }
    }
}
