package com.shvya.callintelligence.calls

import android.content.Context

class TrackingState(context: Context) {
    private val prefs = context.getSharedPreferences("shvya_call_tracking", Context.MODE_PRIVATE)

    var lastCallLogId: Long
        get() = prefs.getLong("last_call_log_id", -1L)
        set(value) = prefs.edit().putLong("last_call_log_id", value).apply()

    var ringingAt: Long
        get() = prefs.getLong("ringing_at", 0L)
        set(value) = prefs.edit().putLong("ringing_at", value).apply()

    var offhookAt: Long
        get() = prefs.getLong("offhook_at", 0L)
        set(value) = prefs.edit().putLong("offhook_at", value).apply()

    var lastPhoneState: String
        get() = prefs.getString("last_phone_state", "") ?: ""
        set(value) = prefs.edit().putString("last_phone_state", value).apply()

    fun clearSession() {
        prefs.edit()
            .putLong("ringing_at", 0L)
            .putLong("offhook_at", 0L)
            .putString("last_phone_state", "")
            .apply()
    }
}
