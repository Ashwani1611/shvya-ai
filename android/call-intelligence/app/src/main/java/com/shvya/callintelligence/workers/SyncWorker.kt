package com.shvya.callintelligence.workers

import android.content.Context
import androidx.work.CoroutineWorker
import androidx.work.WorkerParameters
import com.shvya.callintelligence.data.AppDatabase
import com.shvya.callintelligence.calls.PostCallNotifier
import com.shvya.callintelligence.net.ApiClient
import com.shvya.callintelligence.net.AuthStore
import org.json.JSONObject
import kotlinx.coroutines.CancellationException
import kotlinx.coroutines.sync.Mutex
import kotlinx.coroutines.sync.withLock
import java.io.IOException

class SyncWorker(
    appContext: Context,
    params: WorkerParameters,
) : CoroutineWorker(appContext, params) {
    override suspend fun doWork(): Result = syncMutex.withLock { syncPendingWork() }

    private suspend fun syncPendingWork(): Result {
        val auth = AuthStore(applicationContext)
        if (!auth.canTrack()) return Result.success()
        val session = auth.readSession()
        val sessionGeneration = session.generation
        val accountId = auth.accountId(session) ?: return Result.success()
        fun stillCurrent() = auth.canTrack() && auth.readSession().generation == sessionGeneration

        val dao = AppDatabase.get(applicationContext, accountId).callDao()
        val api = ApiClient(applicationContext, session)
        val registered = try {
            api.registerDevice()
        } catch (exc: Exception) {
            if (exc is CancellationException) throw exc
            return Result.retry()
        }
        if (!registered.successful) return if (
            stillCurrent() && (registered.code !in 400..499 || registered.code in setOf(408, 429))
        ) Result.retry() else Result.success()

        var shouldRetry = false
        dao.pendingQueue(50).forEach { queue ->
            if (!stillCurrent()) return Result.success()
            val call = dao.callById(queue.callLocalId)
            if (call == null) {
                dao.deleteQueue(queue.id)
                return@forEach
            }
            try {
                dao.updateSyncState(
                    call.id,
                    "syncing",
                    queue.retryCount,
                    "",
                    call.remoteCallId,
                )
                val response = api.postCall(call)
                if (!stillCurrent()) return Result.success()
                if (response.successful) {
                    val remoteId = response.confirmedCallId()
                        ?: throw IOException("SHVYA did not confirm the saved call. Retrying safely.")
                    dao.updateSyncState(call.id, "synced", queue.retryCount, "", remoteId)
                    dao.deleteQueue(queue.id)
                    if (remoteId.isNotBlank()) {
                        PostCallNotifier.notifyCaptured(
                            context = applicationContext,
                            callLogId = call.callLogId,
                            contactName = call.contactName,
                            phoneNumber = call.phoneNumber,
                            status = call.status,
                            remoteCallId = remoteId,
                        )
                    }
                } else {
                    val retryCount = queue.retryCount + 1
                    val error = ("HTTP " + response.code + " " + response.body).take(1000)
                    val permanent = response.code in 400..499 &&
                        response.code !in setOf(401, 403, 408, 429)
                    if (permanent) {
                        dao.updateSyncState(call.id, "failed", retryCount, error, call.remoteCallId)
                        dao.deleteQueue(queue.id)
                    } else {
                        dao.updateSyncState(call.id, "pending", retryCount, error, call.remoteCallId)
                        dao.updateQueueRetry(queue.id, retryCount, error)
                        shouldRetry = shouldRetry || response.code !in setOf(401, 403)
                    }
                }
            } catch (exc: Exception) {
                if (exc is CancellationException) throw exc
                val retryCount = queue.retryCount + 1
                val error = (exc.message ?: exc.javaClass.simpleName).take(1000)
                dao.updateSyncState(call.id, "pending", retryCount, error, call.remoteCallId)
                dao.updateQueueRetry(queue.id, retryCount, error)
                shouldRetry = true
            }
        }
        dao.pendingActions(50).forEach { action ->
            if (!stillCurrent()) return Result.success()
            try {
                val payload = JSONObject(action.payload)
                val response = when (action.method.uppercase()) {
                    "PATCH" -> api.authorizedPatch(action.path, payload)
                    else -> api.authorizedPost(action.path, payload)
                }
                if (!stillCurrent()) return Result.success()
                if (response.successful) {
                    dao.deletePendingAction(action.id)
                } else {
                    val retryCount = action.retryCount + 1
                    val error = ("HTTP " + response.code + " " + response.body).take(1000)
                    val permanent = response.code in 400..499 &&
                        response.code !in setOf(401, 403, 408, 429)
                    if (permanent) {
                        dao.deletePendingAction(action.id)
                    } else {
                        dao.updatePendingAction(action.id, retryCount, error)
                        shouldRetry = shouldRetry || response.code !in setOf(401, 403)
                    }
                }
            } catch (exc: Exception) {
                if (exc is CancellationException) throw exc
                val retryCount = action.retryCount + 1
                dao.updatePendingAction(
                    action.id,
                    retryCount,
                    (exc.message ?: exc.javaClass.simpleName).take(1000),
                )
                shouldRetry = true
            }
        }
        return if (shouldRetry) Result.retry() else Result.success()
    }

    companion object {
        // Manual, periodic and post-call sync can overlap. A pending action must be sent once at a time.
        private val syncMutex = Mutex()
    }
}
