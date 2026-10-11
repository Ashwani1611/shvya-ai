package com.shvya.callintelligence.calls

import android.content.BroadcastReceiver
import android.content.Context
import android.content.Intent
import android.os.Build
import com.shvya.callintelligence.net.AuthStore

class BootReceiver : BroadcastReceiver() {
    override fun onReceive(context: Context, intent: Intent) {
        if (intent.action != Intent.ACTION_BOOT_COMPLETED &&
            intent.action != Intent.ACTION_MY_PACKAGE_REPLACED &&
            (Build.VERSION.SDK_INT < Build.VERSION_CODES.N || intent.action != Intent.ACTION_LOCKED_BOOT_COMPLETED)
        ) return
        if (!AuthStore(context).canTrack()) return
        TrackingScheduler.scheduleRecurring(context)
        TrackingScheduler.enqueueReconcile(context)
        TrackingScheduler.enqueueSync(context)
    }
}
