package com.shvya.callintelligence

import android.app.Application
import com.shvya.callintelligence.workers.WorkerScheduler

class ShvyaCallIntelligenceApp : Application() {
    override fun onCreate() {
        super.onCreate()
        WorkerScheduler.schedulePeriodic(this)
    }
}
