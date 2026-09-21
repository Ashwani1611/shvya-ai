package com.shvya.callintelligence.calls

import android.content.Context
import com.shvya.callintelligence.data.AppDatabase
import com.shvya.callintelligence.data.LocalCallEntity
import com.shvya.callintelligence.data.OutboxEventEntity
import com.shvya.callintelligence.device.DeviceIdentity
import org.json.JSONObject
import java.text.SimpleDateFormat
import java.util.Date
import java.util.Locale
import java.util.TimeZone
import java.util.UUID

class CallCaptureRepository(private val context: Context) {
    private val db = AppDatabase.get(context)
    private val reader = CallLogReader(context)

    suspend fun captureLatest(
        session: CallSessionStore.Session?,
        captureSource: String = "phone_state",
    ): LocalCallEntity? {
        val since = (session?.startedAt ?: System.currentTimeMillis()) - 90_000L
        val snapshot = reader.latestSince(
            sinceMillis = since,
            expectedNumber = session?.incomingNumber?.takeIf { it.isNotBlank() },
        ) ?: return null
        return persist(snapshot, session, captureSource)
    }

    suspend fun reconcileSince(sinceMillis: Long): Int {
        var inserted = 0
        reader.since(sinceMillis).asReversed().forEach { snapshot ->
            if (persist(snapshot, null, "call_log_reconcile") != null) inserted += 1
        }
        return inserted
    }

    private suspend fun persist(
        snapshot: CallLogReader.Snapshot,
        session: CallSessionStore.Session?,
        captureSource: String,
    ): LocalCallEntity? {
        if (snapshot.number.isBlank()) return null
        if (db.callDao().byCallLogId(snapshot.callLogId) != null) return null

        val sourceCallId = "calllog:${snapshot.callLogId}:${snapshot.dateMillis}"
        val localId = UUID.randomUUID().toString()
        val endedAt = snapshot.dateMillis + snapshot.durationSeconds * 1000L
        val ringDuration = if (
            session?.ringingAt != null && session.answeredAt != null
        ) {
            ((session.answeredAt - session.ringingAt) / 1000L)
                .coerceAtLeast(0L)
                .toInt()
        } else 0

        val call = LocalCallEntity(
            id = localId,
            sourceCallId = sourceCallId,
            callLogId = snapshot.callLogId,
            phoneNumber = snapshot.number,
            rawPhoneNumber = snapshot.number,
            contactName = snapshot.cachedName,
            direction = snapshot.direction,
            status = snapshot.status,
            calledAtMillis = snapshot.dateMillis,
            startedAtMillis = session?.startedAt ?: snapshot.dateMillis,
            answeredAtMillis = session?.answeredAt,
            endedAtMillis = endedAt,
            ringDurationSeconds = ringDuration,
            durationSeconds = snapshot.durationSeconds,
        )
        db.callDao().upsert(call)

        val eventUuid = UUID.randomUUID().toString()
        val eventType = when (snapshot.status) {
            "missed" -> "missed"
            "rejected" -> "rejected"
            else -> "completed"
        }
        val payload = JSONObject()
            .put("device_id", DeviceIdentity.id(context))
            .put("event_uuid", eventUuid)
            .put("source_call_id", sourceCallId)
            .put("event_type", eventType)
            .put("direction", snapshot.direction)
            .put("status", snapshot.status)
            .put("phone_number", snapshot.number)
            .put("raw_phone_number", snapshot.number)
            .put("contact_name", snapshot.cachedName)
            .put("occurred_at", iso(snapshot.dateMillis))
            .put("started_at", iso(session?.startedAt ?: snapshot.dateMillis))
            .put("ended_at", iso(endedAt))
            .put("ring_duration_seconds", ringDuration)
            .put("duration_seconds", snapshot.durationSeconds)
            .put(
                "metadata",
                JSONObject()
                    .put("call_log_id", snapshot.callLogId)
                    .put("capture_source", captureSource)
                    .put("android_call_type", snapshot.type),
            )

        session?.answeredAt?.let { payload.put("answered_at", iso(it)) }
        session?.ringingAt?.let { payload.put("ringing_at", iso(it)) }

        db.outboxDao().enqueue(
            OutboxEventEntity(
                eventUuid = eventUuid,
                sourceCallId = sourceCallId,
                eventType = eventType,
                payloadJson = payload.toString(),
                occurredAtMillis = snapshot.dateMillis,
            )
        )
        return call
    }

    suspend fun enrich(
        sourceCallId: String,
        notes: String,
        disposition: String,
        followUpAtMillis: Long?,
    ): LocalCallEntity? {
        val current = db.callDao().bySourceCallId(sourceCallId) ?: return null
        val updated = current.copy(
            notes = notes.trim(),
            disposition = disposition.trim(),
            followUpAtMillis = followUpAtMillis,
        )
        db.callDao().upsert(updated)

        val eventUuid = UUID.randomUUID().toString()
        val eventType = when (updated.status) {
            "missed" -> "missed"
            "rejected" -> "rejected"
            else -> "completed"
        }
        val payload = JSONObject()
            .put("device_id", DeviceIdentity.id(context))
            .put("event_uuid", eventUuid)
            .put("source_call_id", updated.sourceCallId)
            .put("event_type", eventType)
            .put("direction", updated.direction)
            .put("status", updated.status)
            .put("phone_number", updated.phoneNumber)
            .put("raw_phone_number", updated.rawPhoneNumber)
            .put("contact_name", updated.contactName)
            .put("occurred_at", iso(System.currentTimeMillis()))
            .put("started_at", iso(updated.startedAtMillis ?: updated.calledAtMillis))
            .put("ended_at", iso(updated.endedAtMillis ?: updated.calledAtMillis))
            .put("ring_duration_seconds", updated.ringDurationSeconds)
            .put("duration_seconds", updated.durationSeconds)
            .put("notes", updated.notes)
            .put("disposition", updated.disposition)
            .put(
                "metadata",
                JSONObject()
                    .put("call_log_id", updated.callLogId)
                    .put("capture_source", "post_call_editor"),
            )

        followUpAtMillis?.let { payload.put("follow_up_at", iso(it)) }
        updated.answeredAtMillis?.let { payload.put("answered_at", iso(it)) }

        db.outboxDao().enqueue(
            OutboxEventEntity(
                eventUuid = eventUuid,
                sourceCallId = updated.sourceCallId,
                eventType = eventType,
                payloadJson = payload.toString(),
                occurredAtMillis = System.currentTimeMillis(),
            )
        )
        return updated
    }

    private fun iso(millis: Long): String {
        val formatter = SimpleDateFormat("yyyy-MM-dd'T'HH:mm:ss.SSS'Z'", Locale.US)
        formatter.timeZone = TimeZone.getTimeZone("UTC")
        return formatter.format(Date(millis))
    }
}
