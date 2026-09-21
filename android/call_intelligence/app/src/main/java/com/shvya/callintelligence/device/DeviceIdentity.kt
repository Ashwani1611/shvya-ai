package com.shvya.callintelligence.device

import android.content.Context
import android.os.Build
import java.util.UUID

object DeviceIdentity {
    fun id(context: Context): String {
        val prefs = context.getSharedPreferences("shvya_device", Context.MODE_PRIVATE)
        return prefs.getString("device_uuid", null)
            ?: UUID.randomUUID().toString().also {
                prefs.edit().putString("device_uuid", it).apply()
            }
    }

    fun displayName(): String =
        listOf(Build.MANUFACTURER, Build.MODEL)
            .filter { it.isNotBlank() }
            .joinToString(" ")
            .ifBlank { "Android device" }
}
