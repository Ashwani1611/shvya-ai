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
import androidx.core.content.ContextCompat
import com.shvya.callintelligence.MainActivity

object PostCallNotifier {
    private const val CHANNEL_ID = "shvya_post_call"
    private const val CHANNEL_NAME = "Post-call assistant"

    fun notifyCaptured(
        context: Context,
        callLogId: Long,
        contactName: String,
        phoneNumber: String,
        status: String,
        remoteCallId: String = "",
    ) {
        if (
            Build.VERSION.SDK_INT >= 33 &&
            ContextCompat.checkSelfPermission(
                context,
                Manifest.permission.POST_NOTIFICATIONS,
            ) != PackageManager.PERMISSION_GRANTED
        ) {
            return
        }

        val manager = context.getSystemService(NotificationManager::class.java)
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.O) {
            manager.createNotificationChannel(
                NotificationChannel(
                    CHANNEL_ID,
                    CHANNEL_NAME,
                    NotificationManager.IMPORTANCE_DEFAULT,
                ).apply {
                    description = "Review notes, disposition and follow-up after a call."
                }
            )
        }

        val openApp = PendingIntent.getActivity(
            context,
            callLogId.hashCode(),
            Intent(context, MainActivity::class.java).apply {
                flags = Intent.FLAG_ACTIVITY_NEW_TASK or Intent.FLAG_ACTIVITY_CLEAR_TOP
                putExtra("post_call_review", true)
                if (remoteCallId.isNotBlank()) {
                    putExtra("open_call_id", remoteCallId)
                }
            },
            PendingIntent.FLAG_UPDATE_CURRENT or PendingIntent.FLAG_IMMUTABLE,
        )

        val callBack = PendingIntent.getActivity(
            context,
            (callLogId.hashCode() * 31) + 7,
            Intent(Intent.ACTION_DIAL, android.net.Uri.fromParts("tel", phoneNumber, null)),
            PendingIntent.FLAG_UPDATE_CURRENT or PendingIntent.FLAG_IMMUTABLE,
        )
        val addNote = if (remoteCallId.isNotBlank()) {
            PendingIntent.getActivity(
                context,
                (callLogId.hashCode() * 31) + 11,
                Intent(context, MainActivity::class.java).apply {
                    flags = Intent.FLAG_ACTIVITY_NEW_TASK or Intent.FLAG_ACTIVITY_CLEAR_TOP
                    putExtra("post_call_review", true)
                    putExtra("open_call_id", remoteCallId)
                    putExtra("call_action", "note")
                },
                PendingIntent.FLAG_UPDATE_CURRENT or PendingIntent.FLAG_IMMUTABLE,
            )
        } else null
        val remindTomorrow = if (remoteCallId.isNotBlank()) {
            PendingIntent.getActivity(
                context,
                (callLogId.hashCode() * 31) + 13,
                Intent(context, MainActivity::class.java).apply {
                    flags = Intent.FLAG_ACTIVITY_NEW_TASK or Intent.FLAG_ACTIVITY_CLEAR_TOP
                    putExtra("post_call_review", true)
                    putExtra("open_call_id", remoteCallId)
                    putExtra("call_action", "remind_tomorrow")
                },
                PendingIntent.FLAG_UPDATE_CURRENT or PendingIntent.FLAG_IMMUTABLE,
            )
        } else null

        val name = contactName.ifBlank { phoneNumber }
        val readableStatus = status.replace('_', ' ')
        val notification = NotificationCompat.Builder(context, CHANNEL_ID)
            .setSmallIcon(android.R.drawable.sym_action_call)
            .setContentTitle("Review call with $name")
            .setContentText("$readableStatus · Add notes, outcome or follow-up in SHVYA.")
            .setStyle(
                NotificationCompat.BigTextStyle().bigText(
                    "SHVYA captured this call safely on-device. Open Call Intelligence to add notes, choose an outcome, set the next follow-up and keep the CRM updated."
                )
            )
            .setContentIntent(openApp)
            .addAction(android.R.drawable.sym_action_call, "Call back", callBack)
            .apply {
                if (addNote != null) {
                    addAction(android.R.drawable.ic_menu_edit, "Add note", addNote)
                }
                if (remindTomorrow != null) {
                    addAction(android.R.drawable.ic_popup_reminder, "Remind tomorrow", remindTomorrow)
                }
                if (addNote == null && remindTomorrow == null) {
                    addAction(android.R.drawable.ic_menu_view, "Review", openApp)
                }
            }
            .setAutoCancel(true)
            .setPriority(NotificationCompat.PRIORITY_DEFAULT)
            .build()

        manager.notify((5200L + (callLogId % 100000L)).toInt(), notification)
    }
}
