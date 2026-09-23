package com.shvya.callintelligence.workers

import android.content.Context
import androidx.work.CoroutineWorker
import androidx.work.WorkerParameters
import com.shvya.callintelligence.net.AuthStore
import com.shvya.callintelligence.calls.CallLogReader
import com.shvya.callintelligence.calls.PostCallNotifier
import com.shvya.callintelligence.calls.TrackingScheduler
import com.shvya.callintelligence.calls.TrackingState
import com.shvya.callintelligence.data.AppDatabase
import com.shvya.callintelligence.data.LocalCall
import java.util.UUID
import kotlin.math.max

class ReconcileWorker(
    appContext: Context,
    params: WorkerParameters,
) : CoroutineWorker(appContext, params) {
    override suspend fun doWork(): Result {
        if (!AuthStore(applicationContext).hasSession()) return Result.success()
        val state = TrackingState(applicationContext)
        val reader = CallLogReader(applicationContext)
        var baseline = state.lastCallLogId
        if (baseline < 0) {
            baseline = reader.maxId().coerceAtLeast(0L)
            state.lastCallLogId = baseline
            return Result.success()
        }

        val rows = reader.readAfter(baseline)
        if (rows.isEmpty()) return Result.success()

        val dao = AppDatabase.get(applicationContext).callDao()
        val latestId = rows.last().callLogId
        val sessionEndedAt = inputData.getLong("session_ended_at", 0L)
        val sessionOffhookAt = inputData.getLong("session_offhook_at", 0L)
        val sessionRingMs = inputData.getLong("session_ring_ms", 0L)

        rows.forEach { row ->
            state.lastCallLogId = max(state.lastCallLogId, row.callLogId)
            if (row.phoneNumber.isBlank()) return@forEach

            val isLatestSession = row.callLogId == latestId && sessionEndedAt > 0
            val endedAt = if (isLatestSession) sessionEndedAt else row.endedAt
            val answeredAt = if (
                isLatestSession &&
                sessionOffhookAt > 0 &&
                row.talkDurationSeconds > 0
            ) sessionOffhookAt else 0L
            val ringSeconds = if (isLatestSession) {
                (sessionRingMs / 1000L).coerceAtLeast(0L).toInt()
            } else 0
            val totalSeconds = max(
                row.talkDurationSeconds,
                ((endedAt - row.startedAt).coerceAtLeast(0L) / 1000L).toInt(),
            )

            val localId = dao.insertCallAndQueue(
                LocalCall(
                    eventUuid = UUID.randomUUID().toString(),
                    callLogId = row.callLogId,
                    sourceCallId = row.sourceCallId,
                    simAccountId = row.simAccountId,
                    phoneNumber = row.phoneNumber,
                    rawPhoneNumber = row.phoneNumber,
                    contactName = row.contactName,
                    direction = row.direction,
                    status = row.status,
                    startedAt = row.startedAt,
                    answeredAt = answeredAt,
                    endedAt = endedAt,
                    ringDurationSeconds = ringSeconds,
                    talkDurationSeconds = row.talkDurationSeconds,
                    totalDurationSeconds = totalSeconds,
                )
            )
            if (localId > 0) {
                PostCallNotifier.notifyCaptured(
                    context = applicationContext,
                    callLogId = row.callLogId,
                    contactName = row.contactName,
                    phoneNumber = row.phoneNumber,
                    status = row.status,
                )
            }
        }
        TrackingScheduler.enqueueSync(applicationContext)
        return Result.success()
    }
}
