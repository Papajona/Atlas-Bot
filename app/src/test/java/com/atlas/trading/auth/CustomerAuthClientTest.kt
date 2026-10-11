package com.atlas.trading.auth

import kotlinx.coroutines.runBlocking
import org.junit.Assert.assertEquals
import org.junit.Test
import okhttp3.mockwebserver.MockResponse
import okhttp3.mockwebserver.MockWebServer
import org.json.JSONObject
import org.junit.Assert.fail

class CustomerAuthClientTest {
    @Test
    fun rejectsInvalidEmailBeforeNetworkRequest() = runBlocking {
        val client = CustomerAuthClient("http://127.0.0.1:1")
        try {
            client.login("not-an-email", "some-password")
            fail("Invalid email must be rejected")
        } catch (expected: IllegalArgumentException) {
            assertEquals("Enter a valid email address", expected.message)
        }
    }

    @Test
    fun rejectsBlankPasswordBeforeNetworkRequest() = runBlocking {
        val client = CustomerAuthClient("http://127.0.0.1:1")
        try {
            client.login("customer@example.test", " ")
            fail("Blank password must be rejected")
        } catch (expected: IllegalArgumentException) {
            assertEquals("Password is required", expected.message)
        }
    }

    @Test
    fun rejectsMissingBackendUrlBeforeNetworkRequest() = runBlocking {
        val client = CustomerAuthClient("")
        try {
            client.login("customer@example.test", "some-password")
            fail("Missing backend URL must be rejected")
        } catch (expected: IllegalArgumentException) {
            assertEquals("Customer authentication service is unavailable", expected.message)
        }
    }
    @Test
    fun successfulSignInReturnsServerIssuedTokensAndUsesBackendProxy() = runBlocking {
        val server = MockWebServer()
        server.enqueue(
            MockResponse().setResponseCode(200).setBody(
                """{"access_token":"server-access-token","refresh_token":"server-refresh-token"}"""
            )
        )
        server.start()
        try {
            val client = CustomerAuthClient(server.url("/").toString())
            val session = client.login(" customer@example.test ", "secret-password")
            assertEquals("server-access-token", session.accessToken)
            assertEquals("server-refresh-token", session.refreshToken)
            val request = server.takeRequest()
            assertEquals("/api/auth/login", request.path)
            assertEquals("POST", request.method)
            val body = JSONObject(request.body.readUtf8())
            assertEquals("customer@example.test", body.getString("email"))
            assertEquals("secret-password", body.getString("password"))
        } finally {
            server.shutdown()
        }
    }

    @Test
    fun signInFailsClosedWhenServerOmitsAccessToken() = runBlocking {
        val server = MockWebServer()
        server.enqueue(MockResponse().setResponseCode(200).setBody("""{"user":{"id":"x"}}"""))
        server.start()
        try {
            val client = CustomerAuthClient(server.url("/").toString())
            try {
                client.login("customer@example.test", "secret-password")
                fail("A session without an access token must be rejected")
            } catch (expected: IllegalStateException) {
                assertEquals("Customer sign-in did not return an access token", expected.message)
            }
        } finally {
            server.shutdown()
        }
    }

}
