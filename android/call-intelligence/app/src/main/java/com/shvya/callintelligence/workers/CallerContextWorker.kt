package com.shvya.callintelligence.workers

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
import androidx.work.CoroutineWorker
import androidx.work.WorkerParameters
import com.shvya.callintelligence.MainActivity
import com.shvya.callintelligence.net.ApiClient
import com.shvya.callintelligence.net.AuthStore
import org.json.JSONObject
import java.net.URLEncoder

class CallerContextWorker(
    appContext: Context,
    params: WorkerParameters,
) : CoroutineWorker(appContext, params) {
    override suspend fun doWork(): Result {
        if (!AuthStore(applicationContext).hasSession()) return Result.success()
        val phone = inputData.getString("phone").orEmpty()
        val digits = phone.filter { it.isDigit() }
        if (digits.length < 6) return Result.success()
        val query = digits.takeLast(minOf(10, digits.length))
        val response = try {
            ApiClient(applicationContext).authorizedGet(
                "api/v1/call-intelligence/leads/list/?q=" +
                    URLEncoder.encode(query, "UTF-8")
            )
        } catch (_: Exception) {
            return Result.success()
        }
        if (!response.successful) return Result.success()
        val payload = runCatching { JSONObject(response.body) }.getOrNull() ?: return Result.success()
        val rows = payload.optJSONArray("results") ?: return Result.success()
        var lead: JSONObject? = null
        for (i in 0 until rows.length()) {
            val item = rows.optJSONObject(i) ?: continue
            val candidate = item.optString("phone").filter { it.isDigit() }
            if (candidate.takeLast(query.length) == query) {
                lead = item
                break
            }
        }
        if (lead != null) notifyLead(lead)
        return Result.success()
    }

    private fun notifyLead(lead: JSONObject) {
        if (
            Build.VERSION.SDK_INT >= 33 &&
            ContextCompat.checkSelfPermission(
                applicationContext,
                Manifest.permission.POST_NOTIFICATIONS,
            ) != PackageManager.PERMISSION_GRANTED
        ) return

        val manager = applicationContext.getSystemService(NotificationManager::class.java)
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.O) {
            manager.createNotificationChannel(
                NotificationChannel(
                    CHANNEL_ID,
                    "Incoming CRM caller",
                    NotificationManager.IMPORTANCE_HIGH,
                ).apply {
                    description = "Shows SHVYA CRM context when a known lead calls."
                }
            )
        }

        val leadId = lead.optString("id")
        val open = PendingIntent.getActivity(
            applicationContext,
            leadId.hashCode(),
            Intent(applicationContext, MainActivity::class.java).apply {
                flags = Intent.FLAG_ACTIVITY_NEW_TASK or Intent.FLAG_ACTIVITY_CLEAR_TOP
                putExtra("open_lead_id", leadId)
            },
            PendingIntent.FLAG_UPDATE_CURRENT or PendingIntent.FLAG_IMMUTABLE,
        )
        val reminder = lead.optJSONObject("reminder")
        val context = buildString {
            append(lead.optString("pipeline"))
            append("  ›  ")
            append(lead.optString("stage"))
            if (reminder != null) {
                append(if (reminder.optBoolean("overdue")) " · Follow-up overdue" else " · Follow-up scheduled")
            }
        }
        manager.notify(
            ("caller-" + leadId).hashCode(),
            NotificationCompat.Builder(applicationContext, CHANNEL_ID)
                .setSmallIcon(android.R.drawable.sym_action_call)
                .setContentTitle("SHVYA lead calling · " + lead.optString("name"))
                .setContentText(context)
                .setStyle(
                    NotificationCompat.BigTextStyle().bigText(
                        context + if (lead.optString("notes").isNotBlank()) {
                            "\n" + lead.optString("notes").take(180)
                        } else ""
                    )
                )
                .setContentIntent(open)
                .addAction(android.R.drawable.ic_menu_view, "Open lead", open)
                .setAutoCancel(true)
                .setPriority(NotificationCompat.PRIORITY_HIGH)
                .build()
        )
    }

    companion object {
        private const val CHANNEL_ID = "shvya_caller_context"
    }
}
