package com.shvya.callintelligence.data

import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertNotEquals
import org.junit.Assert.assertTrue
import org.junit.Test

class AccountStoragePolicyTest {
    @Test fun accountsHaveStableDistinctNonIdentifyingDatabaseFiles() {
        val alice = AccountStoragePolicy.databaseName("alice@example.com", null)
        val bob = AccountStoragePolicy.databaseName("bob@example.com", null)
        assertEquals(alice, AccountStoragePolicy.databaseName("alice@example.com", null))
        assertNotEquals(alice, bob)
        assertFalse(alice.contains("alice"))
        assertTrue(alice.matches(Regex("shvya_call_intelligence_[a-f0-9]{64}\\.db")))
    }

    @Test fun knownStartupOwnerRetainsLegacyQueueOnlyForThatAccount() {
        val owner = AccountStoragePolicy.captureLegacyOwner(null, "account-a")
        assertEquals(AccountStoragePolicy.LEGACY_DATABASE_NAME, AccountStoragePolicy.databaseName("account-a", owner))
        assertNotEquals(AccountStoragePolicy.LEGACY_DATABASE_NAME, AccountStoragePolicy.databaseName("account-b", owner))
        assertEquals(owner, AccountStoragePolicy.captureLegacyOwner(owner, "account-b"))
    }

    @Test fun unknownLegacyQueueCanNeverBeClaimedByLaterLogin() {
        val owner = AccountStoragePolicy.captureLegacyOwner(null, null)
        assertEquals(AccountStoragePolicy.QUARANTINED_OWNER, owner)
        val afterLogin = AccountStoragePolicy.captureLegacyOwner(owner, "new-account")
        assertEquals(owner, afterLogin)
        assertNotEquals(AccountStoragePolicy.LEGACY_DATABASE_NAME, AccountStoragePolicy.databaseName("new-account", afterLogin))
    }

    @Test fun absentOrBlankOwnerFailsClosed() {
        assertNotEquals(AccountStoragePolicy.LEGACY_DATABASE_NAME, AccountStoragePolicy.databaseName("account-a", null))
        assertEquals(AccountStoragePolicy.QUARANTINED_OWNER, AccountStoragePolicy.captureLegacyOwner(null, "  "))
    }

    @Test fun trackingBaselineIsSeparateForBothAccountAndLogin() {
        fun prefs(account: String, generation: String) =
            AccountStoragePolicy.trackingPreferencesName(account, generation, null, null)
        assertEquals(prefs("a", "login-1"), prefs("a", "login-1"))
        assertNotEquals(prefs("a", "login-1"), prefs("b", "login-1"))
        assertNotEquals(prefs("a", "login-1"), prefs("a", "login-2"))
    }

    @Test fun legacyBaselineSurvivesOnlyTheUninterruptedOriginalSession() {
        val owner = AccountStoragePolicy.captureLegacyOwner(null, "account-a")
        fun prefs(account: String, generation: String) =
            AccountStoragePolicy.trackingPreferencesName(account, generation, owner, "legacy")
        assertEquals(AccountStoragePolicy.LEGACY_TRACKING_PREFERENCES, prefs("account-a", "legacy"))
        assertNotEquals(AccountStoragePolicy.LEGACY_TRACKING_PREFERENCES, prefs("account-b", "legacy"))
        assertNotEquals(AccountStoragePolicy.LEGACY_TRACKING_PREFERENCES, prefs("account-a", "new-login"))
    }

    @Test fun identifiersCannotEscapeTheAppStorageDirectory() {
        val filename = AccountStoragePolicy.databaseName("../../other\\account", null)
        assertFalse(filename.contains('/'))
        assertFalse(filename.contains('\\'))
        assertTrue(filename.matches(Regex("shvya_call_intelligence_[a-f0-9]{64}\\.db")))
    }

    @Test(expected = IllegalArgumentException::class)
    fun signedOutIdentityCannotOpenAccountStorage() {
        AccountStoragePolicy.databaseName("", null)
    }
}
