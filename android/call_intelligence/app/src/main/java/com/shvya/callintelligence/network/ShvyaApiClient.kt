package com.shvya.callintelligence.network

import android.content.Context
import android.os.Build
import com.shvya.callintelligence.BuildConfig
import com.shvya.callintelligence.device.DeviceIdentity
import com.shvya.callintelligence.security.SecureTokenStore
import org.json.JSONObject
import java.io.BufferedReader
import java.io.InputStreamReader
import java.net.HttpURLConnection
import java.net.URL
import java.nio.charset.StandardCharsets

class ShvyaApiClient(private val context: Context) {
    private val tokens = SecureTokenStore(context)
    private val baseUrl = BuildConfig.SHVYA_BASE_URL.trimEnd('/')

    data class ApiResponse(val code: Int, val json: JSONObject?, val raw: String) {
        val ok: Boolean get() = code in 200..299
    }

    fun login(email: String, password: String): Boolean {
        val response = request(
            "POST",
            "/api/v1/auth/token/",
            JSONObject().put("email", email.trim()).put("password", password),
            authorized = false,
        )
        if (!response.ok) return false
        val access = response.json?.optString("access").orEmpty()
        val refresh = response.json?.optString("refresh").orEmpty()
        if (access.isBlank() || refresh.isBlank()) return false
        tokens.save(SecureTokenStore.Tokens(access, refresh))
        return true
    }

    fun logout() = tokens.clear()
    fun hasSession(): Boolean = tokens.load() != null

    fun registerDevice(permissions: JSONObject, batteryIgnored: Boolean): ApiResponse {
        val payload = JSONObject()
            .put("device_id", DeviceIdentity.id(context))
            .put("device_name", DeviceIdentity.displayName())
            .put("manufacturer", Build.MANUFACTURER)
            .put("model", Build.MODEL)
            .put("android_version", Build.VERSION.RELEASE ?: "")
            .put("app_version", BuildConfig.VERSION_NAME)
            .put("permissions", permissions)
            .put("battery_optimization_ignored", batteryIgnored)
        return authorizedRequest("POST", "/api/v1/call-intelligence/devices/register/", payload)
    }

    fun heartbeat(permissions: JSONObject, batteryIgnored: Boolean): ApiResponse {
        val payload = JSONObject()
            .put("device_id", DeviceIdentity.id(context))
            .put("app_version", BuildConfig.VERSION_NAME)
            .put("permissions", permissions)
            .put("battery_optimization_ignored", batteryIgnored)
        return authorizedRequest("POST", "/api/v1/call-intelligence/devices/heartbeat/", payload)
    }

    fun sendEvent(payload: JSONObject): ApiResponse =
        authorizedRequest("POST", "/api/v1/call-intelligence/events/", payload)

    private fun authorizedRequest(method: String, path: String, body: JSONObject?): ApiResponse {
        var response = request(method, path, body, authorized = true)
        if (response.code != HttpURLConnection.HTTP_UNAUTHORIZED) return response
        if (!refresh()) return response
        response = request(method, path, body, authorized = true)
        return response
    }

    @Synchronized
    private fun refresh(): Boolean {
        val current = tokens.load() ?: return false
        val response = request(
            "POST",
            "/api/v1/auth/token/refresh/",
            JSONObject().put("refresh", current.refresh),
            authorized = false,
        )
        if (!response.ok) {
            if (response.code == HttpURLConnection.HTTP_UNAUTHORIZED) tokens.clear()
            return false
        }
        val access = response.json?.optString("access").orEmpty()
        if (access.isBlank()) return false
        val rotatedRefresh = response.json?.optString("refresh").orEmpty()
        tokens.updateAccess(access, rotatedRefresh.ifBlank { null })
        return true
    }

    private fun request(
        method: String,
        path: String,
        body: JSONObject?,
        authorized: Boolean,
    ): ApiResponse {
        val connection = (URL("$baseUrl$path").openConnection() as HttpURLConnection).apply {
            requestMethod = method
            connectTimeout = 15_000
            readTimeout = 20_000
            useCaches = false
            setRequestProperty("Accept", "application/json")
            setRequestProperty("Content-Type", "application/json")
            setRequestProperty("User-Agent", "SHVYA-Call-Intelligence/${BuildConfig.VERSION_NAME}")
            if (authorized) {
                tokens.load()?.access?.takeIf { it.isNotBlank() }?.let {
                    setRequestProperty("Authorization", "Bearer $it")
                }
            }
            if (body != null) doOutput = true
        }

        try {
            if (body != null) {
                connection.outputStream.use {
                    it.write(body.toString().toByteArray(StandardCharsets.UTF_8))
                }
            }
            val code = connection.responseCode
            val stream = if (code in 200..299) connection.inputStream else connection.errorStream
            val raw = stream?.use { input ->
                BufferedReader(InputStreamReader(input, StandardCharsets.UTF_8)).readText()
            }.orEmpty()
            val json = raw.takeIf { it.trimStart().startsWith("{") }
                ?.let { runCatching { JSONObject(it) }.getOrNull() }
            return ApiResponse(code, json, raw)
        } catch (error: Exception) {
            return ApiResponse(-1, null, error.message.orEmpty())
        } finally {
            connection.disconnect()
        }
    }
}
