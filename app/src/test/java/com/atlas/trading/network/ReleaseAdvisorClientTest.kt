package com.atlas.trading.network

import kotlinx.coroutines.runBlocking
import okhttp3.mockwebserver.MockResponse
import okhttp3.mockwebserver.MockWebServer
import org.json.JSONObject
import org.junit.Assert.assertEquals
import org.junit.Assert.assertTrue
import org.junit.Test
import java.io.IOException

class ReleaseAdvisorClientTest {
    @Test
    fun sendsLogsAndConfigurationToAuthenticatedServerEndpoint() = runBlocking {
        val server = MockWebServer()
        server.enqueue(MockResponse().setResponseCode(200).setBody(
            """{"run_id":"run-123","status":"succeeded","model":"test-model","diagnostics":"Likely Gradle dependency failure"}"""
        ))
        server.start()
        try {
            val result = ReleaseAdvisorClient(server.url("/").toString()).analyze(
                accessToken = "admin-session",
                logs = "Execution failed for task :app:compileDebugKotlin",
                configuration = "android { compileSdk = 35 }"
            )
            assertEquals("run-123", result.runId)
            assertEquals("succeeded", result.status)
            assertTrue(result.diagnostics.contains("Gradle dependency failure"))
            val request = server.takeRequest()
            assertEquals("/api/admin/release-diagnostics", request.path)
            assertEquals("Bearer admin-session", request.getHeader("Authorization"))
            val body = JSONObject(request.body.readUtf8())
            assertEquals("Execution failed for task :app:compileDebugKotlin", body.getString("logs"))
            assertEquals("android { compileSdk = 35 }", body.getString("configuration"))
        } finally {
            server.shutdown()
        }
    }

    @Test
    fun propagatesServerAuthorizationAndRateLimitErrors() = runBlocking {
        val server = MockWebServer()
        server.enqueue(MockResponse().setResponseCode(403).setBody("""{"detail":"Release advisor access is blocked"}"""))
        server.enqueue(MockResponse().setResponseCode(429).setBody("""{"detail":"Release advisor rate limit reached"}"""))
        server.start()
        try {
            val client = ReleaseAdvisorClient(server.url("/").toString())
            val blocked = runCatching { client.analyze("token", "build failed", "") }.exceptionOrNull()
            assertTrue(blocked is IOException)
            assertTrue(blocked!!.message!!.contains("blocked"))
            val limited = runCatching { client.analyze("token", "build failed", "") }.exceptionOrNull()
            assertTrue(limited is IOException)
            assertTrue(limited!!.message!!.contains("rate limit"))
        } finally {
            server.shutdown()
        }
    }

    @Test
    fun refusesEmptyInputAndMissingAdminTokenBeforeNetworkRequest() = runBlocking {
        val server = MockWebServer()
        server.start()
        try {
            val client = ReleaseAdvisorClient(server.url("/").toString())
            val noToken = runCatching { client.analyze("", "logs", "") }.exceptionOrNull()
            assertTrue(noToken is IllegalArgumentException)
            val noInput = runCatching { client.analyze("admin-token", "", "") }.exceptionOrNull()
            assertTrue(noInput is IllegalArgumentException)
            assertEquals(0, server.requestCount)
        } finally {
            server.shutdown()
        }
    }
}
