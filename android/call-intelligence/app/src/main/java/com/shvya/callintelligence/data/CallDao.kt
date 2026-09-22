package com.shvya.callintelligence.data

import androidx.room.Dao
import androidx.room.Insert
import androidx.room.OnConflictStrategy
import androidx.room.Query
import androidx.room.Transaction

@Dao
abstract class CallDao {
    @Insert(onConflict = OnConflictStrategy.IGNORE)
    abstract suspend fun insertCall(call: LocalCall): Long

    @Insert(onConflict = OnConflictStrategy.IGNORE)
    abstract suspend fun insertQueue(item: SyncQueueItem): Long

    @Query("SELECT * FROM sync_queue ORDER BY priority DESC, createdAt ASC LIMIT :limit")
    abstract suspend fun pendingQueue(limit: Int = 50): List<SyncQueueItem>

    @Query("SELECT * FROM calls WHERE id = :id LIMIT 1")
    abstract suspend fun callById(id: Long): LocalCall?

    @Query("DELETE FROM sync_queue WHERE id = :id")
    abstract suspend fun deleteQueue(id: Long)

    @Query(
        "UPDATE calls SET syncStatus = :status, retryCount = :retryCount, " +
            "lastError = :error, remoteCallId = :remoteId WHERE id = :callId"
    )
    abstract suspend fun updateSyncState(
        callId: Long,
        status: String,
        retryCount: Int,
        error: String,
        remoteId: String = "",
    )

    @Query(
        "UPDATE sync_queue SET retryCount = :retryCount, lastError = :error " +
            "WHERE id = :queueId"
    )
    abstract suspend fun updateQueueRetry(
        queueId: Long,
        retryCount: Int,
        error: String,
    )

    @Query("SELECT COUNT(*) FROM sync_queue")
    abstract suspend fun pendingCount(): Int

    @Transaction
    open suspend fun insertCallAndQueue(call: LocalCall): Long {
        val callId = insertCall(call)
        if (callId > 0) {
            insertQueue(SyncQueueItem(callLocalId = callId))
        }
        return callId
    }
}
