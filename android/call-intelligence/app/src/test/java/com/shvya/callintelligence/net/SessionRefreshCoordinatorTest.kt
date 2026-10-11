package com.shvya.callintelligence.net

import org.junit.Assert.*
import org.junit.Test
import java.io.IOException
import java.util.concurrent.CountDownLatch
import java.util.concurrent.Executors
import java.util.concurrent.TimeUnit
import java.util.concurrent.atomic.AtomicInteger

class SessionRefreshCoordinatorTest {
    private class MemoryStore : SessionTokenStore {
        @Volatile var session = SessionTokens("access-old", "refresh-old", "login-1")
        @Volatile override var sessionRequiresSignIn = false
        var afterReplace: (() -> Unit)? = null
        override fun readSession() = session
        @Synchronized override fun replaceTokens(expected: SessionTokens, access: String, refresh: String): Boolean {
            if (session != expected) return false
            session = expected.copy(access = access, refresh = refresh)
            afterReplace?.invoke()
            return true
        }
        @Synchronized override fun requireSignIn(expected: SessionTokens): Boolean {
            if (session != expected) return false
            sessionRequiresSignIn = true
            return true
        }
    }

    @Test fun concurrentClientsRotateOnlyOnceAndReuseTheNewPair() {
        val store = MemoryStore()
        val initial = store.session
        val requests = AtomicInteger()
        val started = CountDownLatch(1)
        val release = CountDownLatch(1)
        val pool = Executors.newFixedThreadPool(6)
        try {
            val tasks = (1..6).map {
                pool.submit<RefreshOutcome> {
                    SessionRefreshCoordinator(store) { token ->
                        assertEquals("refresh-old", token)
                        requests.incrementAndGet()
                        started.countDown()
                        check(release.await(5, TimeUnit.SECONDS))
                        TokenRefreshReply(200, "access-new", "refresh-new")
                    }.refresh(initial)
                }
            }
            assertTrue(started.await(5, TimeUnit.SECONDS))
            release.countDown()
            tasks.forEach {
                assertEquals(
                    RefreshOutcome.Ready(initial.copy(access = "access-new", refresh = "refresh-new")),
                    it.get(5, TimeUnit.SECONDS),
                )
            }
            assertEquals(1, requests.get())
            assertFalse(store.sessionRequiresSignIn)
        } finally {
            release.countDown()
            pool.shutdownNow()
        }
    }

    @Test fun expiredRefreshRequiresSignInWithoutDiscardingTheAccount() {
        val store = MemoryStore()
        val initial = store.session
        val result = SessionRefreshCoordinator(store) { TokenRefreshReply(401) }.refresh(initial)
        assertEquals(RefreshOutcome.SignInRequired, result)
        assertTrue(store.sessionRequiresSignIn)
        assertEquals(initial, store.session)
    }

    @Test fun permissionsAndTemporaryFailuresNeverRequireSignOut() {
        for (code in listOf(400, 403, 408, 429, 500, 502, 503)) {
            val store = MemoryStore()
            val initial = store.session
            val result = SessionRefreshCoordinator(store) { TokenRefreshReply(code) }.refresh(initial)
            assertEquals(RefreshOutcome.Unavailable(code), result)
            assertEquals(initial, store.session)
            assertFalse(store.sessionRequiresSignIn)
        }
    }

    @Test fun networkFailurePreservesSessionForRetry() {
        val store = MemoryStore()
        val initial = store.session
        try {
            SessionRefreshCoordinator(store) { throw IOException("offline") }.refresh(initial)
            fail("The network failure must reach the caller")
        } catch (_: IOException) {
            assertEquals(initial, store.session)
            assertFalse(store.sessionRequiresSignIn)
        }
    }

    @Test fun malformedSuccessPreservesTokens() {
        val store = MemoryStore()
        val initial = store.session
        assertEquals(
            RefreshOutcome.Unavailable(502),
            SessionRefreshCoordinator(store) { TokenRefreshReply(200) }.refresh(initial),
        )
        assertEquals(initial, store.session)
        assertFalse(store.sessionRequiresSignIn)
    }

    @Test fun refreshWithoutRotationKeepsExistingRefreshToken() {
        val store = MemoryStore()
        val initial = store.session
        SessionRefreshCoordinator(store) { TokenRefreshReply(200, "access-new") }.refresh(initial)
        assertEquals(initial.copy(access = "access-new"), store.session)
    }

    @Test fun responseFromOldLoginCannotOverwriteNewLogin() {
        for (code in listOf(200, 401, 503)) {
            val store = MemoryStore()
            val initial = store.session
            val newSession = SessionTokens("other-access", "other-refresh", "login-2")
            val result = SessionRefreshCoordinator(store) {
                store.session = newSession
                TokenRefreshReply(code, "old-account-access", "old-account-refresh")
            }.refresh(initial)
            assertEquals(RefreshOutcome.SessionChanged, result)
            assertEquals(newSession, store.session)
            assertFalse(store.sessionRequiresSignIn)
        }
    }

    @Test fun inFlightRefreshCannotUndoSignOut() {
        val store = MemoryStore()
        val initial = store.session
        val signedOut = SessionTokens("", "", "signed-out")
        val result = SessionRefreshCoordinator(store) {
            store.session = signedOut
            TokenRefreshReply(200, "new-access", "new-refresh")
        }.refresh(initial)
        assertEquals(RefreshOutcome.SessionChanged, result)
        assertEquals(signedOut, store.session)
    }

    @Test fun oldRequestNeverRefreshesOrRetriesAsAnotherAccount() {
        val store = MemoryStore()
        val old = store.session
        store.session = SessionTokens("other", "other-refresh", "login-2")
        val result = SessionRefreshCoordinator(store) { error("Must not refresh another account") }.refresh(old)
        assertEquals(RefreshOutcome.SessionChanged, result)
    }

    @Test fun alreadyRotatedTokenIsReusedWithoutAnotherNetworkRequest() {
        val store = MemoryStore()
        val old = store.session
        store.session = old.copy(access = "new-access", refresh = "new-refresh")
        val result = SessionRefreshCoordinator(store) { error("Must reuse the existing rotation") }.refresh(old)
        assertEquals(RefreshOutcome.Ready(store.session), result)
    }

    @Test fun rejectedRotatedAccessTokenRequiresSignInOnceWithoutAnotherRefresh() {
        val store = MemoryStore()
        val coordinator = SessionRefreshCoordinator(store) { TokenRefreshReply(200, "new-access", "new-refresh") }
        val ready = coordinator.refresh(store.session) as RefreshOutcome.Ready
        assertTrue(coordinator.rejectUnauthorizedRetry(ready.session, 401))
        assertTrue(store.sessionRequiresSignIn)
        assertEquals(ready.session, store.session)
    }

    @Test fun retryFailureCannotExpireNewerLoginOrTreatPermissionFailureAsExpiry() {
        val store = MemoryStore()
        val initial = store.session
        val coordinator = SessionRefreshCoordinator(store) { error("No refresh expected") }
        assertFalse(coordinator.rejectUnauthorizedRetry(initial, 403))
        assertFalse(coordinator.rejectUnauthorizedRetry(initial, 503))
        store.session = SessionTokens("other-access", "other-refresh", "login-2")
        assertFalse(coordinator.rejectUnauthorizedRetry(initial, 401))
        assertFalse(store.sessionRequiresSignIn)
    }

    @Test fun loginImmediatelyAfterRotationCannotSupplyCredentialsForTheOlderRetry() {
        val store = MemoryStore()
        val original = store.session
        val otherAccount = SessionTokens("other-access", "other-refresh", "login-2")
        store.afterReplace = { store.session = otherAccount }
        val ready = SessionRefreshCoordinator(store) {
            TokenRefreshReply(200, "rotated-access", "rotated-refresh")
        }.refresh(original) as RefreshOutcome.Ready
        assertEquals(original.copy(access = "rotated-access", refresh = "rotated-refresh"), ready.session)
        assertEquals(otherAccount, store.session)
    }
}
