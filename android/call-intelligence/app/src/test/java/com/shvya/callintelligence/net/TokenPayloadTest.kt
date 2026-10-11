package com.shvya.callintelligence.net

import org.junit.Assert.*
import org.junit.Test

class TokenPayloadTest {
    @Test fun loginRequiresBothStringTokens() {
        assertEquals(ParsedTokenPayload("a", "r"), TokenPayload.parse("""{"access":"a","refresh":"r"}""", true))
        assertNull(TokenPayload.parse("""{"access":"a"}""", true))
    }

    @Test fun refreshAllowsOmittedRotationButNeverCoercesMalformedTokenValues() {
        assertEquals(ParsedTokenPayload("a", ""), TokenPayload.parse("""{"access":"a"}""", false))
        for (bad in listOf("null", "123", "{}", "[]", "true", "\"\"", "\"null\"", "\" a \"")) {
            assertNull(TokenPayload.parse("""{"access":$bad,"refresh":"r"}""", false))
            assertNull(TokenPayload.parse("""{"access":"a","refresh":$bad}""", false))
        }
    }
}
