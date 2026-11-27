package com.atlas.trading.ui

import org.junit.Assert.assertEquals
import org.junit.Assert.assertNull
import org.junit.Test

class EquityHistoryTimestampParserTest {
    @Test
    fun parsesZuluAndExplicitUtcOffsetTimestamps() {
        val zulu = parseEquityObservationEpochMillis("2026-11-25T10:00:00Z")
            ?: error("Zulu timestamp should parse")
        val explicitOffset = parseEquityObservationEpochMillis("2026-11-25T10:00:00+00:00")
            ?: error("Explicit UTC offset should parse")

        assertEquals(zulu, explicitOffset)
        assertEquals(
            java.time.OffsetDateTime.parse("2026-11-25T10:00:00Z").toInstant().toEpochMilli(),
            explicitOffset
        )
    }

    @Test
    fun parsesNonZeroOffsetsAndRejectsInvalidTimestamps() {
        val offset = parseEquityObservationEpochMillis("2026-11-25T15:30:00+05:30")
            ?: error("Non-zero offset timestamp should parse")

        assertEquals(
            java.time.OffsetDateTime.parse("2026-11-25T10:00:00Z").toInstant().toEpochMilli(),
            offset
        )
        assertNull(parseEquityObservationEpochMillis("not-a-timestamp"))
    }
}
