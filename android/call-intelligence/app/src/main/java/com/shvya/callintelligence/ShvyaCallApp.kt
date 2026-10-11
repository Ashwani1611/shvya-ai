package com.shvya.callintelligence

import android.app.Application
import androidx.work.Configuration
import com.shvya.callintelligence.calls.TrackingScheduler
import com.shvya.callintelligence.data.AppDatabase
import com.shvya.callintelligence.net.AuthStore

class ShvyaCallApp : Application(), Configuration.Provider {
    override val workManagerConfiguration: Configuration
        get() = Configuration.Builder().build()

    override fun onCreate() {
        super.onCreate()
        // AndroidKeyStore needs the attached Application (onCreate, not attachBaseContext).
        // The manifest disables only WorkManager's automatic initializer so ownership is
        // captured before the first WorkManager.getInstance(context) can start any workers.
        val auth = AuthStore(this)
        val session = auth.readSession()
        AppDatabase.captureLegacyOwnership(
            this,
            if (auth.hasSession()) auth.accountId(session) else null,
            session.generation,
        )
        if (AppDatabase.accountStorageReady && auth.hasSession()) {
            TrackingScheduler.scheduleRecurring(this)
        }
    }
}
