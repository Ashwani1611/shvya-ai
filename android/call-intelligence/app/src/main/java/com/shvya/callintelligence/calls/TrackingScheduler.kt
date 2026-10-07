package com.shvya.callintelligence.calls

import android.Manifest
import android.content.Context
import android.content.Intent
import android.content.pm.PackageManager
import android.os.Build
import androidx.core.content.ContextCompat
import androidx.work.Constraints
import androidx.work.ExistingPeriodicWorkPolicy
import androidx.work.NetworkType
import androidx.work.OneTimeWorkRequestBuilder
import androidx.work.PeriodicWorkRequestBuilder
import androidx.work.WorkManager
import com.shvya.callintelligence.workers.HeartbeatWorker
import com.shvya.callintelligence.workers.ReconcileWorker
import com.shvya.callintelligence.workers.ReminderWorker
import com.shvya.callintelligence.workers.SyncWorker
import java.util.concurrent.TimeUnit

object TrackingScheduler {
    fun activate(context: Context) {
        ensureBaseline(context)
        scheduleRecurring(context)
        enqueueReconcile(context)
        val intent = Intent(context, CallTrackingService::class.java)
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.O) {
            ContextCompat.startForegroundService(context, intent)
        } else {
            context.startService(intent)
        }
    }

    fun scheduleRecurring(context: Context) {
        WorkManager.getInstance(context).enqueueUniquePeriodicWork(
            "shvya-call-reconcile",
            ExistingPeriodicWorkPolicy.UPDATE,
            PeriodicWorkRequestBuilder<ReconcileWorker>(15, TimeUnit.MINUTES).build(),
        )
        val network = Constraints.Builder()
            .setRequiredNetworkType(NetworkType.CONNECTED)
            .build()
        WorkManager.getInstance(context).enqueueUniquePeriodicWork(
            "shvya-call-sync",
            ExistingPeriodicWorkPolicy.UPDATE,
            PeriodicWorkRequestBuilder<SyncWorker>(15, TimeUnit.MINUTES)
                .setConstraints(network)
                .build(),
        )
        WorkManager.getInstance(context).enqueueUniquePeriodicWork(
            "shvya-call-heartbeat",
            ExistingPeriodicWorkPolicy.UPDATE,
            PeriodicWorkRequestBuilder<HeartbeatWorker>(15, TimeUnit.MINUTES)
                .setConstraints(network)
                .build(),
        )
        WorkManager.getInstance(context).enqueueUniquePeriodicWork(
            "shvya-follow-up-reminders",
            ExistingPeriodicWorkPolicy.UPDATE,
            PeriodicWorkRequestBuilder<ReminderWorker>(15, TimeUnit.MINUTES)
                .setConstraints(network)
                .build(),
        )
    }

    fun enqueueReconcile(context: Context) {
        WorkManager.getInstance(context).enqueue(
            OneTimeWorkRequestBuilder<ReconcileWorker>().build()
        )
    }

    fun enqueueSync(context: Context) {
        val network = Constraints.Builder()
            .setRequiredNetworkType(NetworkType.CONNECTED)
            .build()
        WorkManager.getInstance(context).enqueue(
            OneTimeWorkRequestBuilder<SyncWorker>()
                .setConstraints(network)
                .build()
        )
    }

    fun ensureBaseline(context: Context) {
        val state = TrackingState(context)
        if (state.lastCallLogId >= 0) return
        if (
            ContextCompat.checkSelfPermission(
                context,
                Manifest.permission.READ_CALL_LOG,
            ) != PackageManager.PERMISSION_GRANTED
        ) return
        state.lastCallLogId = CallLogReader(context).maxId().coerceAtLeast(0L)
    }
}
