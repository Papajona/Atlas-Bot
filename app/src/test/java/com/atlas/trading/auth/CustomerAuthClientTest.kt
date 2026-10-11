package com.atlas.trading.auth

import kotlinx.coroutines.runBlocking
import org.junit.Assert.assertEquals
import org.junit.Test
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
}
