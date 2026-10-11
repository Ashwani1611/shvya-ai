package com.shvya.callintelligence.net

/** A generation identifies a login, so late responses cannot mutate a newer account session. */
data class SessionTokens(val access: String, val refresh: String, val generation: String)

interface SessionTokenStore {
    fun readSession(): SessionTokens
    fun replaceTokens(expected: SessionTokens, access: String, refresh: String): Boolean
    fun requireSignIn(expected: SessionTokens): Boolean
    val sessionRequiresSignIn: Boolean
}

data class TokenRefreshReply(val code: Int, val access: String = "", val refresh: String = "")

sealed class RefreshOutcome {
    data class Ready(val session: SessionTokens) : RefreshOutcome()
    data class Unavailable(val code: Int) : RefreshOutcome()
    object SignInRequired : RefreshOutcome()
    object SessionChanged : RefreshOutcome()
}

/** One refresh at a time across every Activity, API client and worker in this process. */
class SessionRefreshCoordinator(
    private val store: SessionTokenStore,
    private val exchange: (String) -> TokenRefreshReply,
) {
    fun rejectUnauthorizedRetry(session: SessionTokens, code: Int): Boolean =
        code == 401 && store.requireSignIn(session)

    fun refresh(failedSession: SessionTokens): RefreshOutcome = synchronized(refreshLock) {
        val current = store.readSession()
        if (current.generation != failedSession.generation) return@synchronized RefreshOutcome.SessionChanged
        if (current.refresh.isBlank() || store.sessionRequiresSignIn) {
            return@synchronized RefreshOutcome.SignInRequired
        }
        // Another waiting request already rotated the pair. Reuse it instead of blacklisting it again.
        if (current.access != failedSession.access) return@synchronized RefreshOutcome.Ready(current)

        val result = exchange(current.refresh)
        if (store.readSession() != current) return@synchronized RefreshOutcome.SessionChanged
        when {
            result.code in 200..299 && result.access.isNotBlank() -> {
                val refresh = result.refresh.ifBlank { current.refresh }
                if (store.replaceTokens(current, result.access, refresh)) {
                    RefreshOutcome.Ready(current.copy(access = result.access, refresh = refresh))
                } else RefreshOutcome.SessionChanged
            }
            result.code == 401 -> {
                if (store.requireSignIn(current)) RefreshOutcome.SignInRequired
                else RefreshOutcome.SessionChanged
            }
            // Permission errors, throttling, proxy failures and malformed responses are not logout events.
            else -> RefreshOutcome.Unavailable(if (result.code in 200..299) 502 else result.code)
        }
    }

    companion object {
        private val refreshLock = Any()
    }
}
