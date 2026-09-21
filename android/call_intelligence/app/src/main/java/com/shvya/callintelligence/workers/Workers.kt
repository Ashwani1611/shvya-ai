package com.shvya.callintelligence.workers

import android.content.Context
import androidx.work.BackoffPolicy
import androidx.work.Constraints
import androidx.work.CoroutineWorker
import androidx.work.ExistingPeriodicWorkPolicy
import androidx.work.ExistingWorkPolicy
import androidx.work.NetworkType
import androidx.work.OneTimeWorkRequestBuilder
import androidx.work.PeriodicWorkRequestBuilder
import androidx.work.WorkManager
import androidx.work.WorkerParameters
import com.shvya.callintelligence.calls.CallCaptureRepository
import com.shvya.callintelligence.data.AppDatabase
import com.shvya.callintelligence.network.ShvyaApiClient
import com.shvya.callintelligence.permissions.PermissionController
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.withContext
import org.json.JSONObject
import java.util.concurrent.TimeUnit

class CallSyncWorker(
    appContext: Context,
    params: WorkerParameters,
) : CoroutineWorker(appContext, params) {
    override suspend fun doWork(): Result = withContext(Dispatchers.IO) {
        val api = ShvyaApiClient(applicationContext)
        if (!api.hasSession()) return@withContext Result.success()

        val db = AppDatabase.get(applicationContext)
        val rows = db.outboxDao().pending(50)
        if (rows.isEmpty()) return@withContext Result.success()

        var retryNeeded = false
        for (row in rows) {
            val payload = runCatching { JSONObject(row.payloadJson) }.getOrNull()
            if (payload == null) {
                db.outboxDao().markFailed(row.eventUuid, "Invalid local JSON payload")
                continue
            }

            var response = api.sendEvent(payload)
            if (response.code == 404 && response.raw.contains("Register", ignoreCase = true)) {
                api.registerDevice(
                    PermissionController.statusJson(applicationContext),
                    PermissionController.batteryOptimizationIgnored(applicationContext),
                )
                response = api.sendEvent(payload)
            }

            if (response.ok) {
                db.outboxDao().delete(row.eventUuid)
                db.callDao().markSynced(row.sourceCallId, System.currentTimeMillis())
            } else {
                val message = "HTTP ${response.code}: ${response.raw.take(500)}"
                db.outboxDao().markFailed(row.eventUuid, message)
                if (response.code == -1 || response.code >= 500 || response.code == 401) {
                    retryNeeded = true
                }
            }
        }

        if (db.outboxDao().pendingCount() > 0 && retryNeeded) Result.retry()
        else Result.success()
    }
}

class CallReconcileWorker(
    appContext: Context,
    params: WorkerParameters,
) : CoroutineWorker(appContext, params) {
    override suspend fun doWork(): Result = withContext(Dispatchers.IO) {
        if (!PermissionController.hasCorePermissions(applicationContext)) {
            return@withContext Result.success()
        }
        if (!ShvyaApiClient(applicationContext).hasSession()) {
            return@withContext Result.success()
        }

        val prefs = applicationContext.getSharedPreferences(
            "shvya_reconcile",
            Context.MODE_PRIVATE,
        )
        val now = System.currentTimeMillis()
        val last = prefs.getLong("last_reconcile_at", now - TimeUnit.HOURS.toMillis(24))
        runCatching {
            CallCaptureRepository(applicationContext)
                .reconcileSince(last - TimeUnit.MINUTES.toMillis(2))
        }.onFailure {
            return@withContext Result.retry()
        }

        prefs.edit().putLong("last_reconcile_at", now).apply()
        WorkerScheduler.enqueueSync(applicationContext)
        Result.success()
    }
}

class DeviceHeartbeatWorker(
    appContext: Context,
    params: WorkerParameters,
) : CoroutineWorker(appContext, params) {
    override suspend fun doWork(): Result = withContext(Dispatchers.IO) {
        val api = ShvyaApiClient(applicationContext)
        if (!api.hasSession()) return@withContext Result.success()

        val permissions = PermissionController.statusJson(applicationContext)
        val batteryIgnored = PermissionController.batteryOptimizationIgnored(applicationContext)
        val heartbeat = api.heartbeat(permissions, batteryIgnored)
        if (heartbeat.ok) return@withContext Result.success()

        val registered = api.registerDevice(permissions, batteryIgnored)
        if (registered.ok) Result.success() else Result.retry()
    }
}

object WorkerScheduler {
    private const val SYNC = "shvya-call-sync"
    private const val RECONCILE = "shvya-call-reconcile"
    private const val HEARTBEAT = "shvya-call-heartbeat"

    fun enqueueSync(context: Context) {
        val constraints = Constraints.Builder()
            .setRequiredNetworkType(NetworkType.CONNECTED)
            .build()
        val request = OneTimeWorkRequestBuilder<CallSyncWorker>()
            .setConstraints(constraints)
            .setBackoffCriteria(
                BackoffPolicy.EXPONENTIAL,
                30,
                TimeUnit.SECONDS,
            )
            .build()
        WorkManager.getInstance(context).enqueueUniqueWork(
            SYNC,
            ExistingWorkPolicy.KEEP,
            request,
        )
    }

    fun enqueueReconcile(context: Context) {
        val request = OneTimeWorkRequestBuilder<CallReconcileWorker>().build()
        WorkManager.getInstance(context).enqueueUniqueWork(
            "$RECONCILE-now",
            ExistingWorkPolicy.REPLACE,
            request,
        )
    }

    fun schedulePeriodic(context: Context) {
        val reconcile = PeriodicWorkRequestBuilder<CallReconcileWorker>(
            15,
            TimeUnit.MINUTES,
        ).build()

        val heartbeat = PeriodicWorkRequestBuilder<DeviceHeartbeatWorker>(
            15,
            TimeUnit.MINUTES,
        )
            .setConstraints(
                Constraints.Builder()
                    .setRequiredNetworkType(NetworkType.CONNECTED)
                    .build()
            )
            .build()

        val workManager = WorkManager.getInstance(context)
        workManager.enqueueUniquePeriodicWork(
            RECONCILE,
            ExistingPeriodicWorkPolicy.UPDATE,
            reconcile,
        )
        workManager.enqueueUniquePeriodicWork(
            HEARTBEAT,
            ExistingPeriodicWorkPolicy.UPDATE,
            heartbeat,
        )
    }
}
