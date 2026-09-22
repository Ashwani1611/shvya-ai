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
import org.json.JSONObject
import java.io.BufferedReader
import java.io.InputStreamReader
import java.net.HttpURLConnection
import java.net.URL
import java.text.SimpleDateFormat
import java.util.Date
import java.util.Locale
import java.util.TimeZone

data class ApiResponse(val code: Int, val body: String) {
    val successful: Boolean get() = code in 200..299
}

class ApiClient(private val context: Context) {
    private val auth = AuthStore(context)
    private val baseUrl = BuildConfig.SHVYA_BASE_URL.trimEnd('/') + "/"

    fun login(email: String, password: String): Boolean {
        val response = request(
            "api/v1/auth/token/",
            JSONObject().put("email", email).put("password", password),
            authorized = false,
        )
        if (!response.successful) return false
        val payload = JSONObject(response.body)
        val access = payload.optString("access")
        val refresh = payload.optString("refresh")
        if (access.isBlank() || refresh.isBlank()) return false
        auth.accessToken = access
        auth.refreshToken = refresh
        auth.email = email
        return true
    }

    fun refresh(): Boolean {
        val refreshToken = auth.refreshToken
        if (refreshToken.isBlank()) return false
        val response = request(
            "api/v1/auth/token/refresh/",
            JSONObject().put("refresh", refreshToken),
            authorized = false,
        )
        if (!response.successful) return false
        val payload = JSONObject(response.body)
        val access = payload.optString("access")
        if (access.isBlank()) return false
        auth.accessToken = access
        val rotated = payload.optString("refresh")
        if (rotated.isNotBlank()) auth.refreshToken = rotated
        return true
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
        return authorizedPost("api/v1/call-intelligence/devices/register/", payload)
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
        var response = request(path, payload, authorized = true, method = method)
        if (response.code == 401 && refresh()) {
            response = request(path, payload, authorized = true, method = method)
        }
        if (response.code == 403 && response.body.contains("device_removed")) {
            auth.clear()
            context.stopService(Intent(context, CallTrackingService::class.java))
            WorkManager.getInstance(context).cancelAllWork()
        }
        return response
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

    private fun request(path: String, payload: JSONObject, authorized: Boolean, method: String = "POST"): ApiResponse {
        val connection = (URL(baseUrl + path.trimStart('/')).openConnection() as HttpURLConnection)
        return try {
            connection.requestMethod = method
            connection.connectTimeout = 15000
            connection.readTimeout = 20000
            connection.doOutput = method != "GET"
            connection.setRequestProperty("Content-Type", "application/json")
            connection.setRequestProperty("Accept", "application/json")
            if (authorized) {
                val token = auth.accessToken
                if (token.isNotBlank()) {
                    connection.setRequestProperty("Authorization", "Bearer " + token)
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
