package com.shvya.callintelligence.workers

import android.Manifest
import android.app.NotificationChannel
import android.app.NotificationManager
import android.app.PendingIntent
import android.content.Context
import android.content.Intent
import android.content.pm.PackageManager
import android.net.Uri
import android.os.Build
import androidx.core.app.NotificationCompat
import androidx.core.content.ContextCompat
import androidx.work.CoroutineWorker
import androidx.work.WorkerParameters
import com.shvya.callintelligence.MainActivity
import com.shvya.callintelligence.net.ApiClient
import com.shvya.callintelligence.net.AuthStore
import org.json.JSONObject
import java.text.SimpleDateFormat
import java.util.Locale

class ReminderWorker(
    appContext: Context,
    params: WorkerParameters,
) : CoroutineWorker(appContext, params) {
    override suspend fun doWork(): Result {
        if (!AuthStore(applicationContext).hasSession()) return Result.success()
        if (
            Build.VERSION.SDK_INT >= 33 &&
            ContextCompat.checkSelfPermission(
                applicationContext,
                Manifest.permission.POST_NOTIFICATIONS,
            ) != PackageManager.PERMISSION_GRANTED
        ) return Result.success()

        val response = try {
            ApiClient(applicationContext).authorizedGet(
                "api/v1/call-intelligence/reminders/?page=1"
            )
        } catch (_: Exception) {
            return Result.retry()
        }
        if (!response.successful) {
            return if (response.code in setOf(401, 403)) Result.success() else Result.retry()
        }

        val payload = runCatching { JSONObject(response.body) }.getOrNull() ?: return Result.success()
        val rows = payload.optJSONArray("reminders") ?: return Result.success()
        val now = System.currentTimeMillis()
        val notifyBefore = now + 30L * 60L * 1000L
        val prefs = applicationContext.getSharedPreferences(
            "shvya_reminder_notifications",
            Context.MODE_PRIVATE,
        )
        for (i in 0 until rows.length()) {
            val item = rows.optJSONObject(i) ?: continue
            val dueAt = item.optString("due_at")
            val dueMillis = parseIso(dueAt) ?: continue
            if (dueMillis > notifyBefore) continue
            val reminderId = item.optString("id")
            val key = reminderId + "|" + dueAt
            if (prefs.getBoolean(key, false)) continue
            notifyReminder(item, dueMillis < now)
            prefs.edit().putBoolean(key, true).apply()
        }
        return Result.success()
    }

    private fun notifyReminder(item: JSONObject, overdue: Boolean) {
        val manager = applicationContext.getSystemService(NotificationManager::class.java)
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.O) {
            manager.createNotificationChannel(
                NotificationChannel(
                    CHANNEL_ID,
                    "SHVYA follow-ups",
                    NotificationManager.IMPORTANCE_HIGH,
                ).apply {
                    description = "CRM follow-ups that are due or overdue."
                }
            )
        }

        val id = item.optString("id")
        val lead = item.optString("lead_name").ifBlank { item.optString("phone") }
        val phone = item.optString("phone")
        val open = PendingIntent.getActivity(
            applicationContext,
            id.hashCode(),
            Intent(applicationContext, MainActivity::class.java).apply {
                flags = Intent.FLAG_ACTIVITY_NEW_TASK or Intent.FLAG_ACTIVITY_CLEAR_TOP
                putExtra("open_followups", true)
            },
            PendingIntent.FLAG_UPDATE_CURRENT or PendingIntent.FLAG_IMMUTABLE,
        )
        val snooze = PendingIntent.getActivity(
            applicationContext,
            id.hashCode() + 1,
            Intent(applicationContext, MainActivity::class.java).apply {
                flags = Intent.FLAG_ACTIVITY_NEW_TASK or Intent.FLAG_ACTIVITY_CLEAR_TOP
                putExtra("open_followups", true)
                putExtra("reminder_id", id)
                putExtra("reminder_action", "snooze")
            },
            PendingIntent.FLAG_UPDATE_CURRENT or PendingIntent.FLAG_IMMUTABLE,
        )
        val done = PendingIntent.getActivity(
            applicationContext,
            id.hashCode() + 2,
            Intent(applicationContext, MainActivity::class.java).apply {
                flags = Intent.FLAG_ACTIVITY_NEW_TASK or Intent.FLAG_ACTIVITY_CLEAR_TOP
                putExtra("open_followups", true)
                putExtra("reminder_id", id)
                putExtra("reminder_action", "complete")
            },
            PendingIntent.FLAG_UPDATE_CURRENT or PendingIntent.FLAG_IMMUTABLE,
        )
        val builder = NotificationCompat.Builder(applicationContext, CHANNEL_ID)
            .setSmallIcon(android.R.drawable.ic_popup_reminder)
            .setContentTitle(if (overdue) "Follow-up overdue · " + lead else "Follow-up due · " + lead)
            .setContentText(item.optString("title").ifBlank { "CRM follow-up" })
            .setContentIntent(open)
            .setAutoCancel(true)
            .setPriority(NotificationCompat.PRIORITY_HIGH)
            .addAction(android.R.drawable.ic_popup_sync, "Snooze 30m", snooze)
            .addAction(android.R.drawable.checkbox_on_background, "Done", done)

        if (phone.isNotBlank()) {
            val call = PendingIntent.getActivity(
                applicationContext,
                id.hashCode() + 3,
                Intent(Intent.ACTION_DIAL, Uri.fromParts("tel", phone, null)),
                PendingIntent.FLAG_UPDATE_CURRENT or PendingIntent.FLAG_IMMUTABLE,
            )
            builder.addAction(android.R.drawable.sym_action_call, "Call", call)
        }
        manager.notify(id.hashCode(), builder.build())
    }

    private fun parseIso(value: String): Long? = runCatching {
        val normalized = value.replace(Regex("\\.\\d+(?=[Z+-])"), "")
        SimpleDateFormat("yyyy-MM-dd'T'HH:mm:ssXXX", Locale.US).parse(normalized)?.time
    }.getOrNull()

    companion object {
        private const val CHANNEL_ID = "shvya_followups"
    }
}
