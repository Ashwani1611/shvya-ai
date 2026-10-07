package com.shvya.callintelligence.workers

import android.content.Context
import androidx.work.CoroutineWorker
import androidx.work.WorkerParameters
import com.shvya.callintelligence.data.AppDatabase
import com.shvya.callintelligence.calls.PostCallNotifier
import com.shvya.callintelligence.net.ApiClient
import com.shvya.callintelligence.net.AuthStore
import org.json.JSONObject

class SyncWorker(
    appContext: Context,
    params: WorkerParameters,
) : CoroutineWorker(appContext, params) {
    override suspend fun doWork(): Result {
        if (!AuthStore(applicationContext).hasSession()) return Result.success()

        val dao = AppDatabase.get(applicationContext).callDao()
        val api = ApiClient(applicationContext)
        val registered = try {
            api.registerDevice()
        } catch (_: Exception) {
            return Result.retry()
        }
        if (!registered.successful) return if (AuthStore(applicationContext).hasSession()) Result.retry() else Result.success()

        var shouldRetry = false
        dao.pendingQueue(50).forEach { queue ->
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
                if (response.successful) {
                    val remoteId = try {
                        JSONObject(response.body)
                            .optJSONObject("call")
                            ?.optString("id")
                            .orEmpty()
                    } catch (_: Exception) {
                        ""
                    }
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
                    if (permanent || retryCount >= 10) {
                        dao.updateSyncState(call.id, "failed", retryCount, error, call.remoteCallId)
                        dao.deleteQueue(queue.id)
                    } else {
                        dao.updateSyncState(call.id, "pending", retryCount, error, call.remoteCallId)
                        dao.updateQueueRetry(queue.id, retryCount, error)
                        shouldRetry = true
                    }
                }
            } catch (exc: Exception) {
                val retryCount = queue.retryCount + 1
                val error = (exc.message ?: exc.javaClass.simpleName).take(1000)
                dao.updateSyncState(call.id, "pending", retryCount, error, call.remoteCallId)
                dao.updateQueueRetry(queue.id, retryCount, error)
                shouldRetry = true
            }
        }
        dao.pendingActions(50).forEach { action ->
            try {
                val payload = JSONObject(action.payload)
                val response = when (action.method.uppercase()) {
                    "PATCH" -> api.authorizedPatch(action.path, payload)
                    else -> api.authorizedPost(action.path, payload)
                }
                if (response.successful) {
                    dao.deletePendingAction(action.id)
                } else {
                    val retryCount = action.retryCount + 1
                    val error = ("HTTP " + response.code + " " + response.body).take(1000)
                    val permanent = response.code in 400..499 &&
                        response.code !in setOf(401, 403, 408, 429)
                    if (permanent || retryCount >= 10) {
                        dao.deletePendingAction(action.id)
                    } else {
                        dao.updatePendingAction(action.id, retryCount, error)
                        shouldRetry = true
                    }
                }
            } catch (exc: Exception) {
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
}
