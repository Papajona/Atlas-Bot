package com.atlas.trading.ui

import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.Spacer
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.height
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.rememberScrollState
import androidx.compose.foundation.verticalScroll
import androidx.compose.material3.Button
import androidx.compose.material3.Card
import androidx.compose.material3.CardDefaults
import androidx.compose.material3.CircularProgressIndicator
import androidx.compose.material3.OutlinedTextField
import androidx.compose.material3.Text
import androidx.compose.material3.TextButton
import androidx.compose.runtime.Composable
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.rememberCoroutineScope
import androidx.compose.runtime.setValue
import androidx.compose.ui.Modifier
import androidx.compose.ui.platform.testTag
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.unit.dp
import androidx.compose.ui.unit.sp
import com.atlas.trading.network.ReleaseAdvisorClient
import kotlinx.coroutines.launch

@Composable
fun ReleaseAdvisorScreen(
    backendUrl: String,
    accessToken: String,
    onBack: () -> Unit,
    modifier: Modifier = Modifier
) {
    var buildLogs by remember { mutableStateOf("") }
    var configuration by remember { mutableStateOf("") }
    var diagnostics by remember { mutableStateOf<String?>(null) }
    var error by remember { mutableStateOf<String?>(null) }
    var busy by remember { mutableStateOf(false) }
    var runId by remember { mutableStateOf<String?>(null) }
    val scope = rememberCoroutineScope()
    val theme = com.atlas.trading.ui.theme.LocalAtlasColors.current

    Column(
        modifier = modifier.fillMaxSize().verticalScroll(rememberScrollState()).padding(16.dp)
            .testTag("release_advisor_screen"),
        verticalArrangement = Arrangement.spacedBy(12.dp)
    ) {
        Row(modifier = Modifier.fillMaxWidth(), horizontalArrangement = Arrangement.SpaceBetween) {
            Column(modifier = Modifier.weight(1f)) {
                Text("Release advisor", fontSize = 23.sp, fontWeight = FontWeight.Bold, color = theme.textPrimary)
                Text("Deterministic local-rule analysis of Android build logs and configuration. No external AI service is called; advice only, and no release gate is changed.", color = theme.textMuted, fontSize = 12.sp)
            }
            TextButton(onClick = onBack) { Text("Back") }
        }
        OutlinedTextField(
            value = buildLogs,
            onValueChange = { buildLogs = it },
            label = { Text("Android build logs") },
            placeholder = { Text("Paste relevant Gradle, Kotlin, signing, or CI errors") },
            modifier = Modifier.fillMaxWidth().height(210.dp),
            minLines = 5,
            enabled = !busy
        )
        OutlinedTextField(
            value = configuration,
            onValueChange = { configuration = it },
            label = { Text("Configuration snippet (optional)") },
            placeholder = { Text("Paste non-secret Gradle or workflow configuration") },
            modifier = Modifier.fillMaxWidth().height(150.dp),
            minLines = 3,
            enabled = !busy
        )
        Text("Remove passwords, tokens, keystore data, and customer information before submitting. The server also redacts common secret formats, but this is not a substitute for reviewing the input.", color = theme.textMuted, fontSize = 12.sp)
        Button(
            onClick = {
                scope.launch {
                    busy = true
                    error = null
                    diagnostics = null
                    runId = null
                    try {
                        val result = ReleaseAdvisorClient(backendUrl).analyze(accessToken, buildLogs, configuration)
                        diagnostics = result.diagnostics
                        runId = result.runId
                    } catch (e: Exception) {
                        error = e.message ?: "Release advisor request failed"
                    } finally {
                        busy = false
                    }
                }
            },
            enabled = !busy && (buildLogs.isNotBlank() || configuration.isNotBlank()),
            modifier = Modifier.fillMaxWidth().testTag("release_advisor_analyze")
        ) {
            if (busy) CircularProgressIndicator()
            else Text("Analyze release blockers")
        }
        if (error != null) {
            Card(colors = CardDefaults.cardColors(containerColor = theme.cardSurface), modifier = Modifier.fillMaxWidth()) {
                Text("Request not completed: $error", modifier = Modifier.padding(14.dp), color = theme.redDanger)
            }
        }
        if (diagnostics != null) {
            Card(colors = CardDefaults.cardColors(containerColor = theme.cardSurface), modifier = Modifier.fillMaxWidth()) {
                Column(modifier = Modifier.padding(14.dp), verticalArrangement = Arrangement.spacedBy(8.dp)) {
                    Text("Analysis result", fontWeight = FontWeight.Bold, color = theme.textPrimary)
                    if (!runId.isNullOrBlank()) Text("Audit run: $runId", fontSize = 11.sp, color = theme.textMuted)
                    Text(diagnostics!!, color = theme.textPrimary)
                }
            }
        }
        Spacer(Modifier.height(12.dp))
    }
}
