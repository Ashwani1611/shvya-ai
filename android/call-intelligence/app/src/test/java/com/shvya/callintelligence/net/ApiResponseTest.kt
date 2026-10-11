package com.shvya.callintelligence.net

import org.junit.Assert.*
import org.junit.Test

class ApiResponseTest {
    @Test fun onlyExplicitDeviceRevocationSuspendsTracking() {
        assertTrue(ApiResponse(403, """{"code":"device_removed","detail":"Disconnected"}""").deviceRemoved)
        assertTrue(ApiResponse(403, """{"detail":{"code":"device_removed"}}""").deviceRemoved)
        assertFalse(ApiResponse(403, """{"detail":"Upgrade to unlock Call Intelligence."}""").deviceRemoved)
        assertFalse(ApiResponse(403, """{"detail":"Help article: device_removed"}""").deviceRemoved)
        assertFalse(ApiResponse(500, """{"code":"device_removed"}""").deviceRemoved)
        assertFalse(ApiResponse(403, "<html>device_removed</html>").deviceRemoved)
        assertFalse(ApiResponse(401, """{"code":"token_not_valid"}""").deviceRemoved)
    }

    @Test fun readsFriendlyMessageFromBothBackendErrorShapes() {
        assertEquals("Disconnected", ApiResponse(403, """{"detail":"Disconnected"}""").detailMessage())
        assertEquals("Access denied", ApiResponse(403, """{"error":true,"detail":{"detail":"Access denied"}}""").detailMessage())
        assertEquals("", ApiResponse(502, "<html>Bad gateway</html>").detailMessage())
    }

    @Test fun malformedCallSuccessMustNotRemoveThePendingCall() {
        val id = "01234567-89ab-cdef-0123-456789abcdef"
        assertEquals(id, ApiResponse(201, """{"call":{"id":"$id"}}""").confirmedCallId())
        for (body in listOf("{}", "<html>OK</html>", """{"call":{"id":null}}""", """{"call":{"id":123}}""", """{"call":{"id":"null"}}""")) {
            assertNull(ApiResponse(200, body).confirmedCallId())
        }
        assertNull(ApiResponse(500, """{"call":{"id":"$id"}}""").confirmedCallId())
    }
}
