package com.shvya.callintelligence.calls

import android.Manifest
import android.app.NotificationChannel
import android.app.NotificationManager
import android.app.PendingIntent
import android.content.Context
import android.content.Intent
import android.content.pm.PackageManager
import android.os.Build
import androidx.core.app.NotificationCompat
import androidx.core.app.NotificationManagerCompat
import androidx.core.content.ContextCompat
import com.shvya.callintelligence.PostCallActivity
import com.shvya.callintelligence.data.LocalCallEntity

object PostCallNotifier {
    private const val CHANNEL_ID = "shvya_post_call"

    fun show(context: Context, call: LocalCallEntity) {
        if (
            Build.VERSION.SDK_INT >= Build.VERSION_CODES.TIRAMISU &&
            ContextCompat.checkSelfPermission(
                context,
                Manifest.permission.POST_NOTIFICATIONS,
            ) != PackageManager.PERMISSION_GRANTED
        ) return

        ensureChannel(context)

        val intent = Intent(context, PostCallActivity::class.java).apply {
            putExtra(PostCallActivity.EXTRA_SOURCE_CALL_ID, call.sourceCallId)
            flags = Intent.FLAG_ACTIVITY_NEW_TASK or Intent.FLAG_ACTIVITY_CLEAR_TOP
        }
        val pendingIntent = PendingIntent.getActivity(
            context,
            call.sourceCallId.hashCode(),
            intent,
            PendingIntent.FLAG_UPDATE_CURRENT or PendingIntent.FLAG_IMMUTABLE,
        )

        val label = call.contactName.ifBlank { call.phoneNumber }
        val message = when (call.status) {
            "missed" -> "Missed call captured. Add a callback reminder."
            "rejected" -> "Rejected call captured. Add an outcome or follow-up."
            else -> "Call captured. Add notes, outcome or a follow-up."
        }

        val notification = NotificationCompat.Builder(context, CHANNEL_ID)
            .setSmallIcon(android.R.drawable.sym_action_call)
            .setContentTitle(label)
            .setContentText(message)
            .setStyle(NotificationCompat.BigTextStyle().bigText(message))
            .setPriority(NotificationCompat.PRIORITY_HIGH)
            .setAutoCancel(true)
            .setContentIntent(pendingIntent)
            .addAction(0, "Add call details", pendingIntent)
            .build()

        NotificationManagerCompat.from(context).notify(
            call.sourceCallId.hashCode(),
            notification,
        )
    }

    private fun ensureChannel(context: Context) {
        if (Build.VERSION.SDK_INT < Build.VERSION_CODES.O) return
        val manager = context.getSystemService(NotificationManager::class.java)
        manager.createNotificationChannel(
            NotificationChannel(
                CHANNEL_ID,
                "Post-call actions",
                NotificationManager.IMPORTANCE_HIGH,
            ).apply {
                description = "Add SHVYA notes, outcomes and follow-up after a phone call."
            }
        )
    }
}
