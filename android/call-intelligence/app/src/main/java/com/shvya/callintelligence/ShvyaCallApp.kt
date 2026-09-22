package com.shvya.callintelligence

import android.app.Application
import com.shvya.callintelligence.calls.TrackingScheduler
import com.shvya.callintelligence.net.AuthStore

class ShvyaCallApp : Application() {
    override fun onCreate() {
        super.onCreate()
        if (AuthStore(this).hasSession()) {
            TrackingScheduler.scheduleRecurring(this)
        }
    }
}
