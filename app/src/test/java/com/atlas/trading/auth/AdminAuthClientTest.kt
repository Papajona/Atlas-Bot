package com.atlas.trading.auth

import kotlinx.coroutines.runBlocking
import org.junit.Assert.assertEquals
import org.junit.Assert.fail
import org.junit.Test

class AdminAuthClientTest {
    @Test
    fun signInRejectsBlankEmailBeforeMakingNetworkRequest() = runBlocking {
        val client = AdminAuthClient("http://127.0.0.1:1")
        try {
            client.login("   ", "non-empty-password")
            fail("Blank email must be rejected")
        } catch (expected: IllegalArgumentException) {
            assertEquals("Email and password are required", expected.message)
        }
    }

    @Test
    fun signInRejectsBlankPasswordBeforeMakingNetworkRequest() = runBlocking {
        val client = AdminAuthClient("http://127.0.0.1:1")
        try {
            client.login("operator@example.test", " ")
            fail("Blank password must be rejected")
        } catch (expected: IllegalArgumentException) {
            assertEquals("Email and password are required", expected.message)
        }
    }

    @Test
    fun mfaRejectsNonSixDigitCodeBeforeSessionLookup() = runBlocking {
        val client = AdminAuthClient("http://127.0.0.1:1")
        try {
            client.verifyMfa("12ab")
            fail("Malformed MFA code must be rejected")
        } catch (expected: IllegalArgumentException) {
            assertEquals("Authenticator code must contain 6 digits", expected.message)
        }
    }
}
