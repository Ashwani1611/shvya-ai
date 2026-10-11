package com.shvya.callintelligence.data

import java.security.MessageDigest

/** Stable, non-identifying file names and fail-closed ownership for pre-account storage. */
object AccountStoragePolicy {
    const val LEGACY_DATABASE_NAME = "shvya_call_intelligence.db"
    const val LEGACY_TRACKING_PREFERENCES = "shvya_call_tracking"
    const val QUARANTINED_OWNER = "quarantined"

    fun accountKey(accountId: String): String {
        require(accountId.isNotBlank()) { "An authenticated account is required for local storage" }
        return digest(accountId)
    }

    /** The first startup owns this decision. A later sign-in must never claim unknown data. */
    fun captureLegacyOwner(existingOwner: String?, startupAccountId: String?): String =
        existingOwner ?: startupAccountId?.takeIf(String::isNotBlank)?.let(::accountKey)
            ?: QUARANTINED_OWNER

    fun databaseName(accountId: String, legacyOwner: String?): String {
        val accountKey = accountKey(accountId)
        // Keep the original file in place, including any pending WAL, for its known owner.
        return if (accountKey == legacyOwner) LEGACY_DATABASE_NAME
        else "shvya_call_intelligence_$accountKey.db"
    }

    fun trackingPreferencesName(
        accountId: String,
        sessionGeneration: String,
        legacyOwner: String?,
        legacyGeneration: String?,
    ): String {
        val accountKey = accountKey(accountId)
        require(sessionGeneration.isNotBlank()) { "A login session is required for call tracking" }
        if (accountKey == legacyOwner && sessionGeneration == legacyGeneration) {
            return LEGACY_TRACKING_PREFERENCES
        }
        // Each new login starts at the current device call-log position. Returning to an old
        // account must not import calls made while another account was signed in.
        return "shvya_call_tracking_${accountKey}_${digest(sessionGeneration)}"
    }

    private fun digest(value: String): String = MessageDigest.getInstance("SHA-256")
        .digest(value.toByteArray(Charsets.UTF_8))
        .joinToString("") { "%02x".format(it.toInt() and 0xff) }
}
