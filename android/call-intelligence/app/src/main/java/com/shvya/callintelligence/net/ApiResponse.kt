package com.shvya.callintelligence.net

import org.json.JSONObject
import java.util.UUID

data class ApiResponse(val code: Int, val body: String) {
    val successful: Boolean get() = code in 200..299

    fun confirmedCallId(): String? {
        if (!successful) return null
        return runCatching {
            val id = JSONObject(body).optJSONObject("call")?.opt("id") as? String ?: return@runCatching null
            UUID.fromString(id)
            id
        }.getOrNull()
    }

    val deviceRemoved: Boolean
        get() {
            if (code != 403) return false
            val json = runCatching { JSONObject(body) }.getOrNull() ?: return false
            return json.optString("code") == "device_removed" ||
                json.optJSONObject("detail")?.optString("code") == "device_removed"
        }

    /** Read both DRF's standard shape and SHVYA's wrapped exception shape. */
    fun detailMessage(): String {
        val json = runCatching { JSONObject(body) }.getOrNull() ?: return ""
        val detail = json.opt("detail")
        return when (detail) {
            is String -> detail
            is JSONObject -> detail.optString("detail").takeUnless { it == "null" }.orEmpty()
            else -> ""
        }
    }
}
