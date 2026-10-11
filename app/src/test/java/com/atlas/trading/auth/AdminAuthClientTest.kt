package com.atlas.trading.auth

import kotlinx.coroutines.runBlocking
import org.junit.Assert.assertEquals
import org.junit.Assert.fail
import org.junit.Test
import okhttp3.mockwebserver.MockResponse
import okhttp3.mockwebserver.MockWebServer
import org.json.JSONObject

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
    @Test
    fun adminSignInUsesBackendAndReturnsOnlyServerIssuedSession() = runBlocking {
        val server = MockWebServer()
        server.enqueue(MockResponse().setResponseCode(200).setBody(
            """{"access_token":"admin-server-token","mfa_required":false}"""
        ))
        server.start()
        try {
            val client = AdminAuthClient(server.url("/").toString())
            val result = client.login(" operator@example.test ", "operator-password")
            assertEquals("admin-server-token", result.accessToken)
            assertEquals(null, result.challenge)
            val request = server.takeRequest()
            assertEquals("/api/admin/auth/login", request.path)
            assertEquals("POST", request.method)
            val body = JSONObject(request.body.readUtf8())
            assertEquals("operator@example.test", body.getString("email"))
            assertEquals("operator-password", body.getString("password"))
        } finally {
            server.shutdown()
        }
    }

}
