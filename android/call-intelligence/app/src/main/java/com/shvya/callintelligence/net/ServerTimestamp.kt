package com.shvya.callintelligence.net

import java.text.ParsePosition
import java.text.SimpleDateFormat
import java.util.Locale
import java.util.TimeZone

/** Parses timezone-aware server timestamps on every supported Android version, including API 23. */
object ServerTimestamp {
    private val timestamp = Regex(
        "^(\\d{4}-\\d{2}-\\d{2}T\\d{2}:\\d{2}:\\d{2})(?:\\.(\\d+))?(Z|[+-]\\d{2}:?\\d{2})$"
    )

    fun parseMillis(value: String): Long? {
        val match = timestamp.matchEntire(value) ?: return null
        val offset = match.groupValues[3].let { if (it == "Z") "+0000" else it.replace(":", "") }
        if (offset.substring(1, 3).toInt() > 23 || offset.substring(3, 5).toInt() > 59) return null
        // Django can return microseconds; Date stores milliseconds. Do not interpret .1 as 1 ms.
        val milliseconds = match.groupValues[2].padEnd(3, '0').take(3)
        val normalized = match.groupValues[1] + "." + milliseconds + offset
        val parser = SimpleDateFormat("yyyy-MM-dd'T'HH:mm:ss.SSSZ", Locale.US).apply {
            isLenient = false
            timeZone = TimeZone.getTimeZone("UTC")
        }
        val position = ParsePosition(0)
        val date = parser.parse(normalized, position) ?: return null
        return date.time.takeIf { position.index == normalized.length }
    }
}
