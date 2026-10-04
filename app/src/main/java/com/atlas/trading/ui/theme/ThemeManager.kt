package com.atlas.trading.ui.theme

import android.content.Context
import android.content.SharedPreferences
import androidx.compose.foundation.isSystemInDarkTheme
import androidx.compose.runtime.*
import androidx.compose.ui.graphics.Color

enum class AppThemeMode(val label: String, val icon: String) {
    LIGHT("Light", "☀️"),
    DARK("Dark", "🌙"),
    SYSTEM("System", "📱")
}

data class AtlasThemeColors(
    val isDark: Boolean,
    val bgGradient: List<Color>,
    val orbColor1: Color,
    val orbColor2: Color,
    val orbColor3: Color,
    val cardSurface: Color,
    val cardBorder: Color,
    val cardInnerSurface: Color,
    val textPrimary: Color,
    val textMuted: Color,
    val brandTeal: Color,
    val brandAccent: Color,
    val emeraldAccent: Color,
    val emeraldBadgeBg: Color,
    val emeraldBadgeText: Color,
    val redDanger: Color,
    val redBadgeBg: Color,
    val amberWarning: Color,
    val divider: Color,
    val bottomNavSurface: Color
)

val LightAtlasColors = AtlasThemeColors(
    isDark = false,
    bgGradient = listOf(
        Color(0xFFD3F5E9), // Luminous ambient mint green
        Color(0xFFDFF3FE), // Soft ambient sky blue / cyan
        Color(0xFFEFF9F5), // Mint tint
        Color(0xFFF3F7FA)  // Clean soft base
    ),
    orbColor1 = Color(0xFF34D399).copy(alpha = 0.28f),
    orbColor2 = Color(0xFF38BDF8).copy(alpha = 0.22f),
    orbColor3 = Color(0xFF10B981).copy(alpha = 0.18f),
    cardSurface = Color(0xFFFFFFFF).copy(alpha = 0.90f),
    cardBorder = Color(0xFFFFFFFF).copy(alpha = 0.75f),
    cardInnerSurface = Color(0xFFF8FAFC),
    textPrimary = Color(0xFF0F172A),
    textMuted = Color(0xFF64748B),
    brandTeal = Color(0xFF0D9488),
    brandAccent = Color(0xFF2563EB),
    emeraldAccent = Color(0xFF10B981),
    emeraldBadgeBg = Color(0xFFD1FAE5),
    emeraldBadgeText = Color(0xFF047857),
    redDanger = Color(0xFFEF4444),
    redBadgeBg = Color(0xFFFEE2E2),
    amberWarning = Color(0xFFF59E0B),
    divider = Color(0xFFE2E8F0).copy(alpha = 0.6f),
    bottomNavSurface = Color(0xFFFFFFFF).copy(alpha = 0.92f)
)

val DarkAtlasColors = AtlasThemeColors(
    isDark = true,
    bgGradient = listOf(
        Color(0xFF070B18), // Deep obsidian
        Color(0xFF0A1224), // Midnight Navy
        Color(0xFF08101E), // Subtle dark tint
        Color(0xFF050811)  // Bottom deep black
    ),
    orbColor1 = Color(0xFF10B981).copy(alpha = 0.18f), // Luminous emerald glow
    orbColor2 = Color(0xFF0284C7).copy(alpha = 0.20f), // Neon cyan glow
    orbColor3 = Color(0xFF0D9488).copy(alpha = 0.15f), // Teal glow
    cardSurface = Color(0xFF0E1729).copy(alpha = 0.92f),
    cardBorder = Color(0xFF1E293B).copy(alpha = 0.85f),
    cardInnerSurface = Color(0xFF131F35),
    textPrimary = Color(0xFFF8FAFC),
    textMuted = Color(0xFF94A3B8),
    brandTeal = Color(0xFF14B8A6),
    brandAccent = Color(0xFF38BDF8),
    emeraldAccent = Color(0xFF34D399),
    emeraldBadgeBg = Color(0xFF064E3B).copy(alpha = 0.70f),
    emeraldBadgeText = Color(0xFF34D399),
    redDanger = Color(0xFFF87171),
    redBadgeBg = Color(0xFF450A0A).copy(alpha = 0.70f),
    amberWarning = Color(0xFFFBBF24),
    divider = Color(0xFF1E293B).copy(alpha = 0.8f),
    bottomNavSurface = Color(0xFF0B1222).copy(alpha = 0.95f)
)

val LocalAtlasColors = staticCompositionLocalOf { LightAtlasColors }
val LocalThemeMode = staticCompositionLocalOf { AppThemeMode.LIGHT }
val LocalThemeUpdater = staticCompositionLocalOf<(AppThemeMode) -> Unit> { {} }

object ThemePreferences {
    private const val PREFS_NAME = "atlas_theme_prefs"
    private const val KEY_THEME_MODE = "app_theme_mode"

    fun getThemeMode(context: Context): AppThemeMode {
        val prefs: SharedPreferences = context.getSharedPreferences(PREFS_NAME, Context.MODE_PRIVATE)
        val modeStr = prefs.getString(KEY_THEME_MODE, AppThemeMode.LIGHT.name)
        return try {
            AppThemeMode.valueOf(modeStr ?: AppThemeMode.LIGHT.name)
        } catch (e: Exception) {
            AppThemeMode.LIGHT
        }
    }

    fun setThemeMode(context: Context, mode: AppThemeMode) {
        val prefs: SharedPreferences = context.getSharedPreferences(PREFS_NAME, Context.MODE_PRIVATE)
        prefs.edit().putString(KEY_THEME_MODE, mode.name).apply()
    }
}

@Composable
fun AtlasTheme(
    themeMode: AppThemeMode,
    onThemeChange: (AppThemeMode) -> Unit,
    content: @Composable () -> Unit
) {
    val isSystemDark = isSystemInDarkTheme()
    val isDark = when (themeMode) {
        AppThemeMode.LIGHT -> false
        AppThemeMode.DARK -> true
        AppThemeMode.SYSTEM -> isSystemDark
    }

    val colors = if (isDark) DarkAtlasColors else LightAtlasColors

    CompositionLocalProvider(
        LocalAtlasColors provides colors,
        LocalThemeMode provides themeMode,
        LocalThemeUpdater provides onThemeChange
    ) {
        content()
    }
}
