package com.shvya.callintelligence.calls

import android.Manifest
import android.content.Context
import android.content.pm.PackageManager
import android.provider.CallLog
import androidx.core.content.ContextCompat

class CallLogReader(private val context: Context) {
    data class Snapshot(
        val callLogId: Long,
        val number: String,
        val cachedName: String,
        val type: Int,
        val dateMillis: Long,
        val durationSeconds: Int,
        val direction: String,
        val status: String,
    )

    private val projection = arrayOf(
        CallLog.Calls._ID,
        CallLog.Calls.NUMBER,
        CallLog.Calls.CACHED_NAME,
        CallLog.Calls.TYPE,
        CallLog.Calls.DATE,
        CallLog.Calls.DURATION,
    )

    fun latestSince(sinceMillis: Long, expectedNumber: String? = null): Snapshot? =
        since(sinceMillis, 12).firstOrNull { snapshot ->
            expectedNumber.isNullOrBlank() || numbersLikelyMatch(snapshot.number, expectedNumber)
        }

    fun since(sinceMillis: Long, limit: Int = 200): List<Snapshot> {
        if (
            ContextCompat.checkSelfPermission(context, Manifest.permission.READ_CALL_LOG) !=
            PackageManager.PERMISSION_GRANTED
        ) return emptyList()

        val rows = mutableListOf<Snapshot>()
        runCatching {
            context.contentResolver.query(
                CallLog.Calls.CONTENT_URI,
                projection,
                "${CallLog.Calls.DATE} >= ?",
                arrayOf(sinceMillis.toString()),
                "${CallLog.Calls.DATE} DESC",
            )?.use { cursor ->
                val idIndex = cursor.getColumnIndexOrThrow(CallLog.Calls._ID)
                val numberIndex = cursor.getColumnIndexOrThrow(CallLog.Calls.NUMBER)
                val nameIndex = cursor.getColumnIndexOrThrow(CallLog.Calls.CACHED_NAME)
                val typeIndex = cursor.getColumnIndexOrThrow(CallLog.Calls.TYPE)
                val dateIndex = cursor.getColumnIndexOrThrow(CallLog.Calls.DATE)
                val durationIndex = cursor.getColumnIndexOrThrow(CallLog.Calls.DURATION)

                while (cursor.moveToNext() && rows.size < limit) {
                    val type = cursor.getInt(typeIndex)
                    val duration = cursor.getLong(durationIndex).coerceAtLeast(0L).toInt()
                    val (direction, status) = mapType(type, duration)
                    rows += Snapshot(
                        callLogId = cursor.getLong(idIndex),
                        number = cursor.getString(numberIndex).orEmpty(),
                        cachedName = cursor.getString(nameIndex).orEmpty(),
                        type = type,
                        dateMillis = cursor.getLong(dateIndex),
                        durationSeconds = duration,
                        direction = direction,
                        status = status,
                    )
                }
            }
        }
        return rows
    }

    private fun mapType(type: Int, duration: Int): Pair<String, String> = when (type) {
        CallLog.Calls.INCOMING_TYPE -> "inbound" to "completed"
        CallLog.Calls.OUTGOING_TYPE -> "outbound" to if (duration > 0) "completed" else "no_answer"
        CallLog.Calls.MISSED_TYPE -> "inbound" to "missed"
        CallLog.Calls.REJECTED_TYPE -> "inbound" to "rejected"
        else -> "unknown" to "unknown"
    }

    private fun numbersLikelyMatch(a: String, b: String): Boolean {
        val aDigits = a.filter(Char::isDigit)
        val bDigits = b.filter(Char::isDigit)
        if (aDigits.isBlank() || bDigits.isBlank()) return false
        val tail = minOf(8, aDigits.length, bDigits.length)
        return aDigits.takeLast(tail) == bDigits.takeLast(tail)
    }
}
