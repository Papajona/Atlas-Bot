package com.atlas.trading.ui

import androidx.compose.foundation.Canvas
import androidx.compose.foundation.background
import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.BoxScope
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.runtime.Composable
import androidx.compose.ui.Modifier
import androidx.compose.ui.geometry.Offset
import androidx.compose.ui.graphics.Brush
import androidx.compose.ui.graphics.Color
import com.atlas.trading.ui.theme.LocalAtlasColors

/**
 * Universal ambient background shared by every page across the Atlas Trading application.
 * Dynamically renders the theme's vertical gradient and radiant ambient glowing orbs
 * supporting both Light and Dark mode.
 */
@Composable
fun AppAmbientBackground(
    modifier: Modifier = Modifier,
    content: @Composable BoxScope.() -> Unit
) {
    val themeColors = LocalAtlasColors.current

    Box(
        modifier = modifier
            .fillMaxSize()
            .background(brush = Brush.verticalGradient(colors = themeColors.bgGradient))
    ) {
        Canvas(modifier = Modifier.fillMaxSize()) {
            drawCircle(
                brush = Brush.radialGradient(
                    colors = listOf(themeColors.orbColor1, Color.Transparent),
                    center = Offset(size.width * 0.85f, size.height * 0.10f),
                    radius = size.width * 0.65f
                )
            )
            drawCircle(
                brush = Brush.radialGradient(
                    colors = listOf(themeColors.orbColor2, Color.Transparent),
                    center = Offset(size.width * 0.10f, size.height * 0.35f),
                    radius = size.width * 0.70f
                )
            )
            drawCircle(
                brush = Brush.radialGradient(
                    colors = listOf(themeColors.orbColor3, Color.Transparent),
                    center = Offset(size.width * 0.90f, size.height * 0.70f),
                    radius = size.width * 0.60f
                )
            )
        }

        content()
    }
}

