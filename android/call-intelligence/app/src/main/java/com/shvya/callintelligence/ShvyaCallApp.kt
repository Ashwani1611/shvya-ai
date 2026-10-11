package com.shvya.callintelligence

import android.app.Application
import android.content.Context
import com.shvya.callintelligence.calls.TrackingScheduler
import com.shvya.callintelligence.data.AppDatabase
import com.shvya.callintelligence.net.AuthStore

class ShvyaCallApp : Application() {
    override fun attachBaseContext(base: Context) {
        super.attachBaseContext(base)
        // WorkManager's Startup provider runs before onCreate. Capture the installed session
        // first, so neither a worker nor the next person signing in can claim legacy queues.
        val auth = AuthStore(base)
        val session = auth.readSession()
        AppDatabase.captureLegacyOwnership(
            base,
            if (auth.hasSession()) auth.accountId(session) else null,
            session.generation,
        )
    }

    override fun onCreate() {
        super.onCreate()
        if (AppDatabase.accountStorageReady && AuthStore(this).hasSession()) {
            TrackingScheduler.scheduleRecurring(this)
        }
    }
}
