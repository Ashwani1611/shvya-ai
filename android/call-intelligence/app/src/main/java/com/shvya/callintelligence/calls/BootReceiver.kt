package com.shvya.callintelligence.calls

import android.content.BroadcastReceiver
import android.content.Context
import android.content.Intent
import com.shvya.callintelligence.net.AuthStore

class BootReceiver : BroadcastReceiver() {
    override fun onReceive(context: Context, intent: Intent) {
        if (!AuthStore(context).hasSession()) return
        TrackingScheduler.scheduleRecurring(context)
        TrackingScheduler.enqueueReconcile(context)
        TrackingScheduler.enqueueSync(context)
    }
}
