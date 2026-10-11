package com.shvya.callintelligence.net

import org.junit.Assert.*
import org.junit.Test
import java.time.Instant
import java.util.TimeZone

class ServerTimestampTest {
    private val utc = Instant.parse("2026-10-11T06:00:05Z").toEpochMilli()

    @Test fun parsesUtcAndBothPositiveOffsetFormatsAsTheSameInstant() {
        assertEquals(utc, ServerTimestamp.parseMillis("2026-10-11T06:00:05Z"))
        assertEquals(utc, ServerTimestamp.parseMillis("2026-10-11T11:30:05+05:30"))
        assertEquals(utc, ServerTimestamp.parseMillis("2026-10-11T11:30:05+0530"))
    }

    @Test fun preservesNegativeOffsetAcrossDates() {
        assertEquals(utc, ServerTimestamp.parseMillis("2026-10-11T02:00:05-04:00"))
        assertEquals(utc, ServerTimestamp.parseMillis("2026-10-10T23:00:05-0700"))
    }

    @Test fun preservesMillisecondsAndTruncatesFinerServerPrecision() {
        assertEquals(utc + 100, ServerTimestamp.parseMillis("2026-10-11T06:00:05.1Z"))
        assertEquals(utc + 120, ServerTimestamp.parseMillis("2026-10-11T11:30:05.12+05:30"))
        assertEquals(utc + 123, ServerTimestamp.parseMillis("2026-10-11T06:00:05.123Z"))
        assertEquals(utc + 123, ServerTimestamp.parseMillis("2026-10-11T06:00:05.123456Z"))
    }

    @Test fun refusesMalformedDatesOffsetsOrUnspecifiedTimezones() {
        for (value in listOf(
            "2026-10-11T06:00:05", "2026-10-11T06:00:05+24:00", "2026-10-11T06:00:05+05:60",
            "2026-02-30T06:00:05Z", "2026-10-11T25:00:05Z", "2026-10-11T06:00:05Zjunk",
            "2026-10-11T06:00:05.Z", "not a date", "",
        )) assertNull(value, ServerTimestamp.parseMillis(value))
    }

    @Test fun deviceTimezoneDoesNotChangeTheServerInstant() {
        val original = TimeZone.getDefault()
        try {
            TimeZone.setDefault(TimeZone.getTimeZone("America/Los_Angeles"))
            assertEquals(utc, ServerTimestamp.parseMillis("2026-10-11T11:30:05+05:30"))
            assertEquals(utc, ServerTimestamp.parseMillis("2026-10-11T06:00:05Z"))
        } finally {
            TimeZone.setDefault(original)
        }
    }
}
