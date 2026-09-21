package com.shvya.callintelligence.data

import androidx.room.Dao
import androidx.room.Insert
import androidx.room.OnConflictStrategy
import androidx.room.Query
import androidx.room.Upsert

@Dao
interface CallDao {
    @Upsert
    suspend fun upsert(call: LocalCallEntity)

    @Query("SELECT * FROM local_calls WHERE callLogId = :callLogId LIMIT 1")
    suspend fun byCallLogId(callLogId: Long): LocalCallEntity?

    @Query("SELECT * FROM local_calls WHERE sourceCallId = :sourceCallId LIMIT 1")
    suspend fun bySourceCallId(sourceCallId: String): LocalCallEntity?

    @Query("SELECT * FROM local_calls ORDER BY calledAtMillis DESC LIMIT :limit")
    suspend fun recent(limit: Int = 25): List<LocalCallEntity>

    @Query("UPDATE local_calls SET syncedAtMillis = :syncedAt WHERE sourceCallId = :sourceCallId")
    suspend fun markSynced(sourceCallId: String, syncedAt: Long)
}

@Dao
interface OutboxDao {
    @Insert(onConflict = OnConflictStrategy.IGNORE)
    suspend fun enqueue(event: OutboxEventEntity): Long

    @Query("SELECT * FROM sync_outbox WHERE attempts < 12 ORDER BY createdAtMillis ASC LIMIT :limit")
    suspend fun pending(limit: Int = 50): List<OutboxEventEntity>

    @Query("DELETE FROM sync_outbox WHERE eventUuid = :eventUuid")
    suspend fun delete(eventUuid: String)

    @Query(
        """
        UPDATE sync_outbox
        SET attempts = attempts + 1, lastError = :message
        WHERE eventUuid = :eventUuid
        """
    )
    suspend fun markFailed(eventUuid: String, message: String)

    @Query("SELECT COUNT(*) FROM sync_outbox WHERE attempts < 12")
    suspend fun pendingCount(): Int
}
