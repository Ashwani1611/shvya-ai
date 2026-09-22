package com.shvya.callintelligence.workers

import android.content.Context
import android.os.PowerManager
import androidx.work.CoroutineWorker
import androidx.work.WorkerParameters
import com.shvya.callintelligence.net.ApiClient
import com.shvya.callintelligence.net.AuthStore

class HeartbeatWorker(
    appContext: Context,
    params: WorkerParameters,
) : CoroutineWorker(appContext, params) {
    override suspend fun doWork(): Result {
        if (!AuthStore(applicationContext).hasSession()) return Result.success()
        return try {
            val api = ApiClient(applicationContext)
            val registration = api.registerDevice()
            if (!registration.successful) return Result.retry()
            val powerManager = applicationContext.getSystemService(Context.POWER_SERVICE) as PowerManager
            val ignored = powerManager.isIgnoringBatteryOptimizations(applicationContext.packageName)
            if (api.heartbeat(ignored).successful) Result.success() else Result.retry()
        } catch (_: Exception) {
            Result.retry()
        }
    }
}
