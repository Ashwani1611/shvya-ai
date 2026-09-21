package com.shvya.callintelligence.workers

import android.content.BroadcastReceiver
import android.content.Context
import android.content.Intent
import com.shvya.callintelligence.network.ShvyaApiClient
import com.shvya.callintelligence.permissions.PermissionController

class BootReceiver : BroadcastReceiver() {
    override fun onReceive(context: Context, intent: Intent) {
        WorkerScheduler.schedulePeriodic(context)
        if (
            ShvyaApiClient(context).hasSession() &&
            PermissionController.hasCorePermissions(context)
        ) {
            WorkerScheduler.enqueueReconcile(context)
            WorkerScheduler.enqueueSync(context)
        }
    }
}
