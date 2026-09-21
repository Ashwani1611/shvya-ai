package com.shvya.callintelligence.calls

import android.content.BroadcastReceiver
import android.content.Context
import android.content.Intent
import android.telephony.TelephonyManager
import com.shvya.callintelligence.network.ShvyaApiClient
import com.shvya.callintelligence.permissions.PermissionController
import com.shvya.callintelligence.workers.WorkerScheduler
import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.delay
import kotlinx.coroutines.launch

class CallStateReceiver : BroadcastReceiver() {
    override fun onReceive(context: Context, intent: Intent) {
        if (!PermissionController.hasCorePermissions(context)) return
        if (!ShvyaApiClient(context).hasSession()) return

        val state = intent.getStringExtra(TelephonyManager.EXTRA_STATE) ?: return
        val sessionStore = CallSessionStore(context)
        when (state) {
            TelephonyManager.EXTRA_STATE_RINGING -> {
                sessionStore.onRinging(
                    intent.getStringExtra(TelephonyManager.EXTRA_INCOMING_NUMBER)
                )
            }
            TelephonyManager.EXTRA_STATE_OFFHOOK -> sessionStore.onOffHook()
            TelephonyManager.EXTRA_STATE_IDLE -> finishCall(context, sessionStore)
        }
    }

    private fun finishCall(context: Context, sessionStore: CallSessionStore) {
        val pending = goAsync()
        val session = sessionStore.current()
        CoroutineScope(Dispatchers.IO).launch {
            try {
                val repository = CallCaptureRepository(context.applicationContext)
                var captured = false
                repeat(4) { attempt ->
                    if (!captured) {
                        if (attempt > 0) delay(500L * attempt)
                        captured = repository.captureLatest(session)
                    }
                }
                if (captured) WorkerScheduler.enqueueSync(context)
            } finally {
                sessionStore.clear()
                pending.finish()
            }
        }
    }
}
