package com.atlas.trading

import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.padding
import androidx.compose.material3.Button
import androidx.compose.material3.CircularProgressIndicator
import androidx.compose.material3.OutlinedTextField
import androidx.compose.material3.Surface
import androidx.compose.material3.Text
import androidx.compose.runtime.*
import androidx.compose.ui.Modifier
import androidx.compose.ui.text.input.PasswordVisualTransformation
import androidx.compose.ui.unit.dp
import com.atlas.trading.auth.AdminAuthClient

@Composable
fun AdminLoginScreen(
    error: String?,
    mfaChallenge: AdminAuthClient.MfaChallenge?,
    onLogin: (String, String) -> Unit,
    onVerifyMfa: (String) -> Unit
) {
    var email by remember { mutableStateOf("") }
    var password by remember { mutableStateOf("") }
    var code by remember { mutableStateOf("") }
    var busy by remember { mutableStateOf(false) }

    Surface(modifier = Modifier.fillMaxSize()) {
        Column(
            modifier = Modifier.fillMaxSize().padding(24.dp),
            verticalArrangement = Arrangement.Center
        ) {
            Text("ATLAS SECURE OPERATOR LOGIN")
            Text("Risk controls require an authenticated administrator session.")
            if (mfaChallenge == null) {
                OutlinedTextField(
                    value = email,
                    onValueChange = { email = it },
                    modifier = Modifier.fillMaxWidth(),
                    label = { Text("Administrator email") },
                    singleLine = true
                )
                OutlinedTextField(
                    value = password,
                    onValueChange = { password = it },
                    modifier = Modifier.fillMaxWidth(),
                    label = { Text("Password") },
                    visualTransformation = PasswordVisualTransformation(),
                    singleLine = true
                )
                Button(
                    onClick = { busy = true; onLogin(email, password); busy = false },
                    enabled = email.isNotBlank() && password.isNotBlank(),
                    modifier = Modifier.fillMaxWidth().padding(top = 12.dp)
                ) { Text("SIGN IN") }
            } else {
                Text("Enter the 6-digit Google Authenticator code.")
                OutlinedTextField(
                    value = code,
                    onValueChange = { value -> code = value.filter(Char::isDigit).take(6) },
                    modifier = Modifier.fillMaxWidth(),
                    label = { Text("Authenticator code") },
                    singleLine = true
                )
                Button(
                    onClick = { busy = true; onVerifyMfa(code); busy = false },
                    enabled = code.length == 6,
                    modifier = Modifier.fillMaxWidth().padding(top = 12.dp)
                ) { Text("VERIFY AAL2") }
            }
            if (busy) CircularProgressIndicator(modifier = Modifier.padding(top = 12.dp))
            if (!error.isNullOrBlank()) Text(error, modifier = Modifier.padding(top = 12.dp))
        }
    }
}
