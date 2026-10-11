package com.shvya.callintelligence.net

import org.json.JSONObject

data class ParsedTokenPayload(val access: String, val refresh: String)

object TokenPayload {
    fun parse(body: String, requireRefresh: Boolean): ParsedTokenPayload? {
        val payload = runCatching { JSONObject(body) }.getOrNull() ?: return null
        fun token(key: String): String? = (payload.opt(key) as? String)
            ?.takeIf { it.isNotBlank() && it == it.trim() && !it.equals("null", ignoreCase = true) }
        val access = token("access") ?: return null
        val refresh = if (payload.has("refresh") || requireRefresh) token("refresh") ?: return null else ""
        return ParsedTokenPayload(access, refresh)
    }
}
