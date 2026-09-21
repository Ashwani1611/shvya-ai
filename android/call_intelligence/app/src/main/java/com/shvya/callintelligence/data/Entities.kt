package com.shvya.callintelligence.data

import androidx.room.Entity
import androidx.room.Index
import androidx.room.PrimaryKey

@Entity(
    tableName = "local_calls",
    indices = [
        Index(value = ["sourceCallId"], unique = true),
        Index(value = ["callLogId"], unique = true),
        Index(value = ["calledAtMillis"]),
    ],
)
data class LocalCallEntity(
    @PrimaryKey val id: String,
    val sourceCallId: String,
    val callLogId: Long?,
    val phoneNumber: String,
    val rawPhoneNumber: String,
    val contactName: String,
    val direction: String,
    val status: String,
    val calledAtMillis: Long,
    val startedAtMillis: Long?,
    val answeredAtMillis: Long?,
    val endedAtMillis: Long?,
    val ringDurationSeconds: Int,
    val durationSeconds: Int,
    val syncedAtMillis: Long? = null,
)

@Entity(
    tableName = "sync_outbox",
    indices = [
        Index(value = ["createdAtMillis"]),
        Index(value = ["sourceCallId"]),
    ],
)
data class OutboxEventEntity(
    @PrimaryKey val eventUuid: String,
    val sourceCallId: String,
    val eventType: String,
    val payloadJson: String,
    val occurredAtMillis: Long,
    val attempts: Int = 0,
    val lastError: String = "",
    val createdAtMillis: Long = System.currentTimeMillis(),
)
