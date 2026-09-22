package com.shvya.callintelligence.calls

import android.app.NotificationChannel
import android.app.NotificationManager
import android.app.Service
import android.content.Intent
import android.os.Build
import android.os.IBinder
import androidx.core.app.NotificationCompat
import com.shvya.callintelligence.R

class CallTrackingService : Service() {
    override fun onCreate() {
        super.onCreate()
        createChannel()
        val notification = NotificationCompat.Builder(this, CHANNEL_ID)
            .setSmallIcon(android.R.drawable.sym_action_call)
            .setContentTitle("SHVYA Call Intelligence")
            .setContentText("Call capture and CRM sync are active")
            .setOngoing(true)
            .setPriority(NotificationCompat.PRIORITY_LOW)
            .build()
        startForeground(NOTIFICATION_ID, notification)
        TrackingScheduler.scheduleRecurring(this)
    }

    override fun onStartCommand(intent: Intent?, flags: Int, startId: Int): Int {
        TrackingScheduler.enqueueSync(this)
        return START_STICKY
    }

    override fun onTaskRemoved(rootIntent: Intent?) {
        TrackingScheduler.enqueueReconcile(this)
        TrackingScheduler.enqueueSync(this)
        super.onTaskRemoved(rootIntent)
    }

    override fun onBind(intent: Intent?): IBinder? = null

    private fun createChannel() {
        if (Build.VERSION.SDK_INT < Build.VERSION_CODES.O) return
        val manager = getSystemService(NotificationManager::class.java)
        manager.createNotificationChannel(
            NotificationChannel(
                CHANNEL_ID,
                getString(R.string.tracking_channel),
                NotificationManager.IMPORTANCE_LOW,
            ).apply {
                description = getString(R.string.tracking_channel_description)
            }
        )
    }

    companion object {
        private const val CHANNEL_ID = "shvya_call_tracking"
        private const val NOTIFICATION_ID = 4101
    }
}
