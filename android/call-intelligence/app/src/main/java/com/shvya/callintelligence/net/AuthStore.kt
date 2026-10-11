package com.shvya.callintelligence.net

import android.content.Context
import android.util.Base64
import androidx.security.crypto.EncryptedSharedPreferences
import androidx.security.crypto.MasterKey
import java.util.UUID
import org.json.JSONObject
import com.shvya.callintelligence.data.AppDatabase

class AuthStore(context: Context) : SessionTokenStore {
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

    val accessToken: String
        get() = prefs.getString("access", "") ?: ""

    val refreshToken: String
        get() = prefs.getString("refresh", "") ?: ""

    val email: String
        get() = prefs.getString("email", "") ?: ""

    fun hasSession(): Boolean = accessToken.isNotBlank() && refreshToken.isNotBlank()

    val deviceBlocked: Boolean get() = prefs.getBoolean("device_blocked", false)

    override val sessionRequiresSignIn: Boolean get() = prefs.getBoolean("sign_in_required", false) ||
        (accessToken.isNotBlank() && accountId() == null)

    val sessionMessage: String get() = when {
        sessionRequiresSignIn -> "Your session has expired. Sign in again to continue."
        deviceBlocked -> "This device was disconnected by your administrator. Ask them to reconnect it, then refresh the connection."
        else -> ""
    }

    fun canTrack(): Boolean = AppDatabase.accountStorageReady && hasSession() && !deviceBlocked && !sessionRequiresSignIn

    override fun readSession(): SessionTokens = synchronized(stateLock) {
        SessionTokens(accessToken, refreshToken, prefs.getString("generation", "legacy").orEmpty())
    }

    /** Used only to partition local storage; the server remains the authority for authentication. */
    fun accountId(session: SessionTokens = readSession()): String? = runCatching {
        val encoded = session.access.split('.').getOrNull(1) ?: return@runCatching null
        val payload = JSONObject(String(Base64.decode(encoded, Base64.URL_SAFE or Base64.NO_WRAP), Charsets.UTF_8))
        when (val id = payload.opt("user_id")) {
            is String -> id.takeIf { it.isNotBlank() && it != "null" }
            is Number -> id.toString()
            else -> null
        }
    }.getOrNull()

    fun saveLogin(access: String, refresh: String, email: String, expectedGeneration: String): Boolean = synchronized(stateLock) {
        if (!AppDatabase.accountStorageReady) return@synchronized false
        if (readSession().generation != expectedGeneration) return@synchronized false
        // Write the token pair atomically: no worker can observe half of a rotated session.
        prefs.edit()
            .putString("access", access)
            .putString("refresh", refresh)
            .putString("email", email)
            .putString("generation", UUID.randomUUID().toString())
            .putBoolean("sign_in_required", false)
            .apply()
        true
    }

    override fun replaceTokens(expected: SessionTokens, access: String, refresh: String): Boolean = synchronized(stateLock) {
        if (readSession() != expected) return@synchronized false
        prefs.edit().putString("access", access).putString("refresh", refresh).apply()
        true
    }

    override fun requireSignIn(expected: SessionTokens): Boolean = synchronized(stateLock) {
        if (readSession() != expected) return@synchronized false
        prefs.edit().putBoolean("sign_in_required", true).apply()
        true
    }

    fun setDeviceBlocked(expected: SessionTokens, blocked: Boolean): Boolean = synchronized(stateLock) {
        if (readSession().generation != expected.generation) return@synchronized false
        prefs.edit().putBoolean("device_blocked", blocked).apply()
        true
    }

    fun clear() = synchronized(stateLock) {
        // A removed device remains blocked even after local sign-out, until the server revalidates it.
        val blocked = deviceBlocked
        prefs.edit().clear()
            .putString("generation", UUID.randomUUID().toString())
            .putBoolean("device_blocked", blocked)
            .apply()
    }

    companion object {
        private val stateLock = Any()
    }
}
