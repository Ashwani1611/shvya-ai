package com.shvya.callintelligence.net

import android.content.Context
import androidx.security.crypto.EncryptedSharedPreferences
import androidx.security.crypto.MasterKey

class AuthStore(context: Context) {
    private val masterKey = MasterKey.Builder(context)
        .setKeyScheme(MasterKey.KeyScheme.AES256_GCM)
        .build()

    private val prefs = EncryptedSharedPreferences.create(
        context,
        "shvya_call_auth",
        masterKey,
        EncryptedSharedPreferences.PrefKeyEncryptionScheme.AES256_SIV,
        EncryptedSharedPreferences.PrefValueEncryptionScheme.AES256_GCM,
    )

    var accessToken: String
        get() = prefs.getString("access", "") ?: ""
        set(value) = prefs.edit().putString("access", value).apply()

    var refreshToken: String
        get() = prefs.getString("refresh", "") ?: ""
        set(value) = prefs.edit().putString("refresh", value).apply()

    var email: String
        get() = prefs.getString("email", "") ?: ""
        set(value) = prefs.edit().putString("email", value).apply()

    fun hasSession(): Boolean = accessToken.isNotBlank() && refreshToken.isNotBlank()

    fun clear() {
        prefs.edit().clear().apply()
    }
}
