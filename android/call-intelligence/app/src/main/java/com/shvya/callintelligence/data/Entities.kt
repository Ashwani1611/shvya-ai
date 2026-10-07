package com.shvya.callintelligence.data

import androidx.room.Entity
import androidx.room.ForeignKey
import androidx.room.Index
import androidx.room.PrimaryKey

@Entity(
    tableName = "calls",
    indices = [
        Index(value = ["eventUuid"], unique = true),
        Index(value = ["callLogId"], unique = true),
        Index(value = ["syncStatus"]),
    ],
)
data class LocalCall(
    @PrimaryKey(autoGenerate = true) val id: Long = 0,
    val eventUuid: String,
    val callLogId: Long,
    val sourceCallId: String,
    val simAccountId: String = "",
    val phoneNumber: String,
    val rawPhoneNumber: String,
    val contactName: String,
    val direction: String,
    val status: String,
    val startedAt: Long,
    val answeredAt: Long = 0,
    val endedAt: Long,
    val ringDurationSeconds: Int = 0,
    val talkDurationSeconds: Int = 0,
    val totalDurationSeconds: Int = 0,
    val syncStatus: String = "pending",
    val retryCount: Int = 0,
    val lastError: String = "",
    val remoteCallId: String = "",
)

@Entity(
    tableName = "sync_queue",
    foreignKeys = [
        ForeignKey(
            entity = LocalCall::class,
            parentColumns = ["id"],
            childColumns = ["callLocalId"],
            onDelete = ForeignKey.CASCADE,
        ),
    ],
    indices = [
        Index(value = ["callLocalId"], unique = true),
        Index(value = ["priority", "createdAt"]),
    ],
)
data class SyncQueueItem(
    @PrimaryKey(autoGenerate = true) val id: Long = 0,
    val callLocalId: Long,
    val actionType: String = "UPSERT_CALL",
    val priority: Int = 10,
    val retryCount: Int = 0,
    val lastError: String = "",
    val createdAt: Long = System.currentTimeMillis(),
)


@Entity(
    tableName = "pending_actions",
    indices = [Index(value = ["createdAt"])],
)
data class PendingAction(
    @PrimaryKey(autoGenerate = true) val id: Long = 0,
    val path: String,
    val method: String,
    val payload: String,
    val label: String = "",
    val retryCount: Int = 0,
    val lastError: String = "",
    val createdAt: Long = System.currentTimeMillis(),
)
