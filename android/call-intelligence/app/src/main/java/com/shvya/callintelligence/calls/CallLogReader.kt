package com.shvya.callintelligence.calls

import android.Manifest
import android.content.Context
import android.content.pm.PackageManager
import android.provider.CallLog
import androidx.core.content.ContextCompat

data class DeviceCall(
    val callLogId: Long,
    val sourceCallId: String,
    val phoneNumber: String,
    val contactName: String,
    val direction: String,
    val status: String,
    val startedAt: Long,
    val endedAt: Long,
    val talkDurationSeconds: Int,
)

class CallLogReader(private val context: Context) {
    fun maxId(): Long {
        if (!hasPermission()) return -1L
        val projection = arrayOf(CallLog.Calls._ID)
        context.contentResolver.query(
            CallLog.Calls.CONTENT_URI,
            projection,
            null,
            null,
            CallLog.Calls._ID + " DESC",
        )?.use { cursor ->
            if (cursor.moveToFirst()) return cursor.getLong(0)
        }
        return -1L
    }

    fun readAfter(afterId: Long): List<DeviceCall> {
        if (!hasPermission()) return emptyList()
        val projection = arrayOf(
            CallLog.Calls._ID,
            CallLog.Calls.NUMBER,
            CallLog.Calls.CACHED_NAME,
            CallLog.Calls.TYPE,
            CallLog.Calls.DATE,
            CallLog.Calls.DURATION,
        )
        val calls = mutableListOf<DeviceCall>()
        context.contentResolver.query(
            CallLog.Calls.CONTENT_URI,
            projection,
            CallLog.Calls._ID + " > ?",
            arrayOf(afterId.toString()),
            CallLog.Calls._ID + " ASC",
        )?.use { cursor ->
            val idIndex = cursor.getColumnIndexOrThrow(CallLog.Calls._ID)
            val numberIndex = cursor.getColumnIndexOrThrow(CallLog.Calls.NUMBER)
            val nameIndex = cursor.getColumnIndexOrThrow(CallLog.Calls.CACHED_NAME)
            val typeIndex = cursor.getColumnIndexOrThrow(CallLog.Calls.TYPE)
            val dateIndex = cursor.getColumnIndexOrThrow(CallLog.Calls.DATE)
            val durationIndex = cursor.getColumnIndexOrThrow(CallLog.Calls.DURATION)

            while (cursor.moveToNext() && calls.size < 200) {
                val id = cursor.getLong(idIndex)
                val number = cursor.getString(numberIndex) ?: ""
                val name = cursor.getString(nameIndex) ?: ""
                val type = cursor.getInt(typeIndex)
                val startedAt = cursor.getLong(dateIndex)
                val duration = cursor.getLong(durationIndex).coerceAtLeast(0L).toInt()
                val direction = when (type) {
                    CallLog.Calls.OUTGOING_TYPE -> "outgoing"
                    else -> "incoming"
                }
                val status = when (type) {
                    CallLog.Calls.MISSED_TYPE -> "missed"
                    CallLog.Calls.REJECTED_TYPE, CallLog.Calls.BLOCKED_TYPE -> "rejected"
                    CallLog.Calls.OUTGOING_TYPE ->
                        if (duration > 0) "answered" else "no_answer"
                    CallLog.Calls.INCOMING_TYPE ->
                        if (duration > 0) "answered" else "no_answer"
                    else -> "unknown"
                }
                val endedAt = startedAt + (duration * 1000L)
                calls += DeviceCall(
                    callLogId = id,
                    sourceCallId = "call-log:" + id + ":" + startedAt,
                    phoneNumber = number,
                    contactName = name,
                    direction = direction,
                    status = status,
                    startedAt = startedAt,
                    endedAt = endedAt,
                    talkDurationSeconds = duration,
                )
            }
        }
        return calls
    }

    private fun hasPermission(): Boolean =
        ContextCompat.checkSelfPermission(
            context,
            Manifest.permission.READ_CALL_LOG,
        ) == PackageManager.PERMISSION_GRANTED
}
