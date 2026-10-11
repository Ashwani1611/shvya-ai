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
        val auth = AuthStore(applicationContext)
        if (!auth.canTrack()) return Result.success()
        val session = auth.readSession()
        return try {
            val api = ApiClient(applicationContext, session)
            val registration = api.registerDevice()
            if (!registration.successful) return if (
                AuthStore(applicationContext).canTrack() &&
                (registration.code !in 400..499 || registration.code in setOf(408, 429))
            ) Result.retry() else Result.success()
            val powerManager = applicationContext.getSystemService(Context.POWER_SERVICE) as PowerManager
            val ignored = powerManager.isIgnoringBatteryOptimizations(applicationContext.packageName)
            val response = api.heartbeat(ignored)
            if (response.successful || !auth.canTrack() || response.code in setOf(401, 403, 404)) Result.success()
            else Result.retry()
        } catch (_: Exception) {
            Result.retry()
        }
    }
}
