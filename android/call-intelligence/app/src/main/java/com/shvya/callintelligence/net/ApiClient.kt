package com.shvya.callintelligence.net

import android.Manifest
import android.content.Context
import android.content.Intent
import com.shvya.callintelligence.calls.CallTrackingService
import androidx.work.WorkManager
import android.content.pm.PackageManager
import android.os.Build
import android.provider.Settings
import androidx.core.content.ContextCompat
import com.shvya.callintelligence.BuildConfig
import com.shvya.callintelligence.data.LocalCall
import com.shvya.callintelligence.data.AppDatabase
import org.json.JSONObject
import java.io.BufferedReader
import java.io.InputStreamReader
import java.net.HttpURLConnection
import java.net.URL
import java.text.SimpleDateFormat
import java.util.Date
import java.util.Locale
import java.util.TimeZone

data class LoginResult(val successful: Boolean, val message: String = "")

class ApiClient(private val context: Context, private val expectedSession: SessionTokens? = null) {
    private val auth = AuthStore(context)
    private val baseUrl = BuildConfig.SHVYA_BASE_URL.trimEnd('/') + "/"

    fun login(email: String, password: String): LoginResult {
        if (!AppDatabase.accountStorageReady) {
            return LoginResult(false, "Could not prepare secure local storage. Restart the app and try again.")
        }
        val initialGeneration = auth.readSession().generation
        val response = request(
            "api/v1/auth/token/",
            JSONObject().put("email", email).put("password", password),
            authorized = false,
        )
        if (!response.successful) {
            val detail = response.detailMessage()
            val message = when (response.code) {
                400, 401 -> "Email or password is incorrect."
                403 -> detail.ifBlank { "This account cannot use the SHVYA mobile workspace." }
                in 500..599 -> "SHVYA is temporarily unavailable. Please try again."
                else -> detail.ifBlank { "Could not sign in to SHVYA (HTTP " + response.code + ")." }
            }
            return LoginResult(false, message)
        }
        val payload = TokenPayload.parse(response.body, requireRefresh = true)
            ?: return LoginResult(false, "SHVYA returned an invalid sign-in response. Please try again.")
        val access = payload.access
        val refresh = payload.refresh
        if (auth.accountId(SessionTokens(access, refresh, initialGeneration)) == null) {
            return LoginResult(false, "SHVYA could not create a secure mobile session. Please try again.")
        }
        if (!auth.saveLogin(access, refresh, email, initialGeneration)) {
            return LoginResult(false, "Your account changed while signing in. Please try again.")
        }
        return LoginResult(true)
    }

    fun refresh(): Boolean {
        return refreshSession(auth.readSession()) is RefreshOutcome.Ready
    }

    private fun refreshSession(session: SessionTokens): RefreshOutcome {
        val outcome = SessionRefreshCoordinator(auth) { refreshToken ->
            val response = request(
                "api/v1/auth/token/refresh/",
                JSONObject().put("refresh", refreshToken),
                authorized = false,
            )
            val payload = TokenPayload.parse(response.body, requireRefresh = false)
            TokenRefreshReply(
                response.code,
                payload?.access.orEmpty(),
                payload?.refresh.orEmpty(),
            )
        }.refresh(session)
        if (outcome is RefreshOutcome.SignInRequired && auth.readSession().generation == session.generation) {
            stopTracking()
        }
        return outcome
    }

    fun registerDevice(): ApiResponse {
        val packageInfo = context.packageManager.getPackageInfo(context.packageName, 0)
        val payload = JSONObject()
            .put("device_id", deviceId())
            .put("name", Build.MANUFACTURER + " " + Build.MODEL)
            .put("manufacturer", Build.MANUFACTURER)
            .put("model", Build.MODEL)
            .put("android_version", Build.VERSION.RELEASE ?: "")
            .put("app_version", packageInfo.versionName ?: "")
            .put("permissions", permissionSnapshot())
            .put("battery_optimization_ignored", false)
        val session = auth.readSession()
        return authorizedPost("api/v1/call-intelligence/devices/register/", payload).also { response ->
            if (response.successful) auth.setDeviceBlocked(session, false)
        }
    }

    fun heartbeat(batteryOptimizationIgnored: Boolean): ApiResponse {
        val payload = JSONObject()
            .put("device_id", deviceId())
            .put("permissions", permissionSnapshot())
            .put("battery_optimization_ignored", batteryOptimizationIgnored)
        return authorizedPost("api/v1/call-intelligence/devices/heartbeat/", payload)
    }

    fun postCall(call: LocalCall): ApiResponse {
        val eventType = when (call.status) {
            "missed" -> "missed"
            "rejected" -> "rejected"
            "busy" -> "busy"
            "no_answer" -> "no_answer"
            "failed" -> "failed"
            else -> "completed"
        }
        val payload = JSONObject()
            .put("event_uuid", call.eventUuid)
            .put("device_id", deviceId())
            .put("source_call_id", call.sourceCallId)
            .put("sim_account_id", call.simAccountId)
            .put("phone_number", call.phoneNumber)
            .put("raw_phone_number", call.rawPhoneNumber)
            .put("contact_name", call.contactName)
            .put("direction", call.direction)
            .put("status", call.status)
            .put("event_type", eventType)
            .put("occurred_at", iso(call.endedAt))
            .put("started_at", iso(call.startedAt))
            .put("answered_at", if (call.answeredAt > 0) iso(call.answeredAt) else JSONObject.NULL)
            .put("ended_at", iso(call.endedAt))
            .put("ring_duration_seconds", call.ringDurationSeconds)
            .put("talk_duration_seconds", call.talkDurationSeconds)
            .put("total_duration_seconds", call.totalDurationSeconds)
            .put("app_version", context.packageManager.getPackageInfo(context.packageName, 0).versionName ?: "")
        return authorizedPost("api/v1/call-intelligence/events/", payload)
    }

    fun authorizedGet(path: String): ApiResponse = authorizedRequest(path, JSONObject(), "GET")

    fun authorizedPatch(path: String, payload: JSONObject): ApiResponse = authorizedRequest(path, payload, "PATCH")

    fun authorizedPost(path: String, payload: JSONObject): ApiResponse = authorizedRequest(path, payload, "POST")

    private fun authorizedRequest(path: String, payload: JSONObject, method: String): ApiResponse {
        val session = auth.readSession()
        if (expectedSession != null && expectedSession.generation != session.generation) return sessionChangedResponse()
        if (session.access.isBlank() || auth.sessionRequiresSignIn) {
            return ApiResponse(401, "{\"code\":\"sign_in_required\",\"detail\":\"Sign in again to continue.\"}")
        }
        if (auth.deviceBlocked && (path.endsWith("events/") || path.endsWith("devices/heartbeat/"))) {
            return ApiResponse(403, "{\"code\":\"device_removed\",\"detail\":\"This device has been disconnected.\"}")
        }
        var response = request(path, payload, authorized = true, method = method, accessToken = session.access)
        if (response.code == 401) {
            when (val outcome = refreshSession(session)) {
                is RefreshOutcome.Ready -> {
                    response = request(
                        path, payload, authorized = true, method = method, accessToken = outcome.session.access,
                    )
                    if (SessionRefreshCoordinator(auth) { error("No further refresh") }
                            .rejectUnauthorizedRetry(outcome.session, response.code)) {
                        stopTracking()
                    }
                }
                is RefreshOutcome.Unavailable -> return ApiResponse(
                    outcome.code,
                    "{\"detail\":\"Could not renew the connection. Please try again.\"}",
                )
                is RefreshOutcome.SessionChanged -> return sessionChangedResponse()
                is RefreshOutcome.SignInRequired -> return ApiResponse(
                    401, "{\"code\":\"sign_in_required\",\"detail\":\"Your session has expired. Sign in again.\"}",
                )
            }
        }
        if (auth.readSession().generation != session.generation) return sessionChangedResponse()
        if (response.deviceRemoved && auth.setDeviceBlocked(session, true)) {
            stopTracking()
        }
        return response
    }

    private fun sessionChangedResponse() = ApiResponse(
        409, "{\"code\":\"session_changed\",\"detail\":\"Your account changed. Refresh this screen.\"}",
    )

    private fun stopTracking() {
        context.stopService(Intent(context, CallTrackingService::class.java))
        WorkManager.getInstance(context).cancelAllWork()
    }

    fun deviceId(): String =
        Settings.Secure.getString(context.contentResolver, Settings.Secure.ANDROID_ID)
            ?: "unknown-android-device"

    private fun permissionSnapshot(): JSONObject {
        fun granted(permission: String): Boolean =
            ContextCompat.checkSelfPermission(context, permission) == PackageManager.PERMISSION_GRANTED

        return JSONObject()
            .put("read_phone_state", granted(Manifest.permission.READ_PHONE_STATE))
            .put("read_call_log", granted(Manifest.permission.READ_CALL_LOG))
            .put("read_contacts", granted(Manifest.permission.READ_CONTACTS))
            .put(
                "read_phone_numbers",
                if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.O) {
                    granted(Manifest.permission.READ_PHONE_NUMBERS)
                } else true,
            )
    }

    private fun request(
        path: String,
        payload: JSONObject,
        authorized: Boolean,
        method: String = "POST",
        accessToken: String = "",
    ): ApiResponse {
        val connection = (URL(baseUrl + path.trimStart('/')).openConnection() as HttpURLConnection)
        return try {
            connection.requestMethod = method
            // Never follow a sign-in/proxy redirect with a bearer credential or a write payload.
            connection.instanceFollowRedirects = false
            connection.connectTimeout = 15000
            connection.readTimeout = 20000
            connection.doOutput = method != "GET"
            connection.setRequestProperty("Content-Type", "application/json")
            connection.setRequestProperty("Accept", "application/json")
            if (authorized) {
                if (accessToken.isNotBlank()) {
                    connection.setRequestProperty("Authorization", "Bearer " + accessToken)
                }
            }
            if (method != "GET") connection.outputStream.use { stream ->
                stream.write(payload.toString().toByteArray(Charsets.UTF_8))
            }
            val code = connection.responseCode
            val input = if (code in 200..299) connection.inputStream else connection.errorStream
            val body = if (input != null) {
                BufferedReader(InputStreamReader(input)).use { it.readText() }
            } else ""
            ApiResponse(code, body)
        } finally {
            connection.disconnect()
        }
    }

    private fun iso(value: Long): String {
        val formatter = SimpleDateFormat("yyyy-MM-dd'T'HH:mm:ss.SSS'Z'", Locale.US)
        formatter.timeZone = TimeZone.getTimeZone("UTC")
        return formatter.format(Date(value))
    }
}
