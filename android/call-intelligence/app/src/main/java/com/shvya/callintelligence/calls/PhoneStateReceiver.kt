package com.shvya.callintelligence.calls

import android.content.BroadcastReceiver
import android.content.Context
import android.content.Intent
import android.telephony.TelephonyManager
import androidx.work.OneTimeWorkRequestBuilder
import androidx.work.WorkManager
import androidx.work.workDataOf
import com.shvya.callintelligence.net.AuthStore
import com.shvya.callintelligence.workers.ReconcileWorker
import java.util.concurrent.TimeUnit

class PhoneStateReceiver : BroadcastReceiver() {
    override fun onReceive(context: Context, intent: Intent) {
        if (!AuthStore(context).hasSession()) return
        val phoneState = intent.getStringExtra(TelephonyManager.EXTRA_STATE) ?: return
        val state = TrackingState(context)
        if (phoneState == state.lastPhoneState) return

        val now = System.currentTimeMillis()
        when (phoneState) {
            TelephonyManager.EXTRA_STATE_RINGING -> {
                state.ringingAt = now
                state.offhookAt = 0L
            }
            TelephonyManager.EXTRA_STATE_OFFHOOK -> {
                state.offhookAt = now
            }
            TelephonyManager.EXTRA_STATE_IDLE -> {
                val ringAt = state.ringingAt
                val offhookAt = state.offhookAt
                val ringMillis = when {
                    ringAt <= 0L -> 0L
                    offhookAt > ringAt -> offhookAt - ringAt
                    now > ringAt -> now - ringAt
                    else -> 0L
                }
                val request = OneTimeWorkRequestBuilder<ReconcileWorker>()
                    .setInitialDelay(1500, TimeUnit.MILLISECONDS)
                    .setInputData(
                        workDataOf(
                            "session_ended_at" to now,
                            "session_offhook_at" to offhookAt,
                            "session_ring_ms" to ringMillis,
                        )
                    )
                    .build()
                WorkManager.getInstance(context).enqueue(request)
                state.clearSession()
            }
        }
        state.lastPhoneState = phoneState
    }
}
