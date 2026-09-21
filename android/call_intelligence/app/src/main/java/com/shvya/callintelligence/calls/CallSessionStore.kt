package com.shvya.callintelligence.calls

import android.content.Context

class CallSessionStore(context: Context) {
    private val prefs = context.getSharedPreferences("shvya_active_call", Context.MODE_PRIVATE)

    data class Session(
        val startedAt: Long,
        val ringingAt: Long?,
        val answeredAt: Long?,
        val incomingNumber: String,
    )

    fun onRinging(number: String?) {
        val now = System.currentTimeMillis()
        prefs.edit()
            .putLong("started_at", now)
            .putLong("ringing_at", now)
            .putString("incoming_number", number.orEmpty())
            .apply()
    }

    fun onOffHook() {
        val now = System.currentTimeMillis()
        val existingStart = prefs.getLong("started_at", 0L)
        prefs.edit()
            .putLong("started_at", if (existingStart > 0L) existingStart else now)
            .putLong("answered_at", now)
            .apply()
    }

    fun current(): Session? {
        val started = prefs.getLong("started_at", 0L)
        if (started <= 0L) return null
        val ringing = prefs.getLong("ringing_at", 0L).takeIf { it > 0L }
        val answered = prefs.getLong("answered_at", 0L).takeIf { it > 0L }
        return Session(
            startedAt = started,
            ringingAt = ringing,
            answeredAt = answered,
            incomingNumber = prefs.getString("incoming_number", "").orEmpty(),
        )
    }

    fun clear() {
        prefs.edit().clear().apply()
    }
}
