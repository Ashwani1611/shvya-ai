package com.shvya.callintelligence

import android.graphics.Color
import android.graphics.Typeface
import android.graphics.drawable.GradientDrawable
import android.os.Bundle
import android.view.Gravity
import android.view.View
import android.widget.Button
import android.widget.EditText
import android.widget.LinearLayout
import android.widget.ScrollView
import android.widget.TextView
import androidx.appcompat.app.AppCompatActivity
import com.shvya.callintelligence.data.AppDatabase
import com.shvya.callintelligence.network.ShvyaApiClient
import com.shvya.callintelligence.permissions.PermissionController
import com.shvya.callintelligence.workers.WorkerScheduler
import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.Job
import kotlinx.coroutines.cancel
import kotlinx.coroutines.launch
import kotlinx.coroutines.withContext

class MainActivity : AppCompatActivity() {
    private val scope = CoroutineScope(Job() + Dispatchers.Main)
    private lateinit var api: ShvyaApiClient
    private lateinit var root: LinearLayout
    private var statusMessage: String = ""

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        api = ShvyaApiClient(this)
        render()
    }

    override fun onResume() {
        super.onResume()
        if (::api.isInitialized && api.hasSession()) render()
    }

    override fun onDestroy() {
        scope.cancel()
        super.onDestroy()
    }

    private fun render() {
        val scroll = ScrollView(this).apply {
            setBackgroundColor(Color.rgb(245, 245, 247))
            isFillViewport = true
        }
        root = LinearLayout(this).apply {
            orientation = LinearLayout.VERTICAL
            setPadding(dp(24), dp(42), dp(24), dp(48))
        }
        scroll.addView(root)
        setContentView(scroll)

        if (api.hasSession()) renderDashboard() else renderLogin()
    }

    private fun renderLogin() {
        eyebrow("SHVYA CALL INTELLIGENCE")
        title("Every call.\nAlready in context.")
        paragraph(
            "Sign in with your SHVYA employee account. Your organization and CRM access are resolved by the server."
        )

        val email = input("Work email", android.text.InputType.TYPE_TEXT_VARIATION_EMAIL_ADDRESS)
        val password = input(
            "Password",
            android.text.InputType.TYPE_CLASS_TEXT or android.text.InputType.TYPE_TEXT_VARIATION_PASSWORD,
        )
        button("Sign in", primary = true) {
            val emailValue = email.text.toString().trim()
            val passwordValue = password.text.toString()
            if (emailValue.isBlank() || passwordValue.isBlank()) {
                statusMessage = "Enter your work email and password."
                render()
                return@button
            }
            statusMessage = "Signing in…"
            render()
            scope.launch {
                val ok = withContext(Dispatchers.IO) { api.login(emailValue, passwordValue) }
                statusMessage = if (ok) "Signed in. Finish Android permissions to start capture."
                    else "Sign-in failed. Check your credentials and SHVYA access."
                if (ok) {
                    WorkerScheduler.schedulePeriodic(this@MainActivity)
                    registerDevice()
                }
                render()
            }
        }

        statusCard()
    }

    private fun renderDashboard() {
        eyebrow("SHVYA CALL INTELLIGENCE")
        title("Your phone.\nConnected to CRM.")
        paragraph(
            "Calls stay on this Android device first, then sync through the durable outbox when the network is available."
        )

        val coreReady = PermissionController.hasCorePermissions(this)
        val batteryReady = PermissionController.batteryOptimizationIgnored(this)

        card {
            rowLabel("Phone & Call Log", if (coreReady) "Ready" else "Permission needed", coreReady)
            rowLabel("Background battery access", if (batteryReady) "Allowed" else "Recommended", batteryReady)
            rowLabel("Secure SHVYA session", "Active", true)
        }

        button(
            if (coreReady) "Review Android permissions" else "Enable Call Intelligence",
            primary = true,
        ) {
            PermissionController.requestRuntime(this)
        }

        button(
            if (batteryReady) "Battery access enabled" else "Allow background battery access",
            primary = false,
        ) {
            PermissionController.requestBatteryExemption(this)
        }

        button("Sync calls now", primary = false) {
            WorkerScheduler.enqueueReconcile(this)
            WorkerScheduler.enqueueSync(this)
            statusMessage = "Call reconciliation and sync queued."
            render()
        }

        sectionTitle("How it works")
        feature("1", "Capture", "Incoming, outgoing, missed and rejected SIM calls are reconciled from Android Call Log.")
        feature("2", "Store", "Room keeps call evidence and unsent events on the employee device.")
        feature("3", "Sync", "JWT-authenticated events are retried without duplicating CRM calls.")
        feature("4", "Match", "SHVYA finds the lead inside the signed-in user’s organization and permitted pipeline.")
        feature("5", "Act", "Call notes, CRM activity and follow-up reminders stay attached to the lead.")

        sectionTitle("Recent local calls")
        val loading = TextView(this).apply {
            text = "Loading local call history…"
            setTextColor(Color.rgb(110, 110, 115))
            textSize = 14f
            setPadding(0, dp(4), 0, dp(16))
        }
        root.addView(loading)
        scope.launch {
            val recent = withContext(Dispatchers.IO) {
                AppDatabase.get(this@MainActivity).callDao().recent(8)
            }
            loading.visibility = View.GONE
            recent.forEach { call ->
                val label = call.contactName.ifBlank { call.phoneNumber }
                val state = "${call.direction.replaceFirstChar { it.uppercase() }} · ${call.status.replace('_', ' ')} · ${call.durationSeconds}s"
                feature("☎", label, state)
            }
            if (recent.isEmpty()) {
                paragraph("No captured calls yet. Make or receive a call after permissions are enabled.")
            }
        }

        button("Sign out of this device", primary = false) {
            api.logout()
            statusMessage = "Signed out. New phone calls will not be captured for SHVYA."
            scope.launch(Dispatchers.IO) {
                AppDatabase.get(this@MainActivity).clearAllTables()
            }
            render()
        }

        statusCard()

        if (coreReady) {
            scope.launch { registerDevice() }
        }
    }

    private suspend fun registerDevice() {
        withContext(Dispatchers.IO) {
            api.registerDevice(
                PermissionController.statusJson(this@MainActivity),
                PermissionController.batteryOptimizationIgnored(this@MainActivity),
            )
        }
    }

    override fun onRequestPermissionsResult(
        requestCode: Int,
        permissions: Array<out String>,
        grantResults: IntArray,
    ) {
        super.onRequestPermissionsResult(requestCode, permissions, grantResults)
        if (requestCode == PermissionController.REQUEST_CODE) {
            statusMessage = if (PermissionController.hasCorePermissions(this)) {
                WorkerScheduler.enqueueReconcile(this)
                WorkerScheduler.enqueueSync(this)
                "Call Intelligence is enabled on this Android device."
            } else {
                "Phone and Call Log permissions are required for automatic capture."
            }
            render()
        }
    }

    private fun eyebrow(value: String) {
        root.addView(TextView(this).apply {
            text = value
            setTextColor(Color.rgb(0, 113, 227))
            textSize = 12f
            typeface = Typeface.DEFAULT_BOLD
            letterSpacing = .08f
            setPadding(0, 0, 0, dp(12))
        })
    }

    private fun title(value: String) {
        root.addView(TextView(this).apply {
            text = value
            setTextColor(Color.rgb(29, 29, 31))
            textSize = 45f
            typeface = Typeface.create(Typeface.DEFAULT, Typeface.BOLD)
            setLineSpacing(0f, .92f)
            setPadding(0, 0, 0, dp(18))
        })
    }

    private fun paragraph(value: String) {
        root.addView(TextView(this).apply {
            text = value
            setTextColor(Color.rgb(110, 110, 115))
            textSize = 17f
            setLineSpacing(dp(4).toFloat(), 1f)
            setPadding(0, 0, 0, dp(22))
        })
    }

    private fun input(hintValue: String, inputTypeValue: Int): EditText {
        val view = EditText(this).apply {
            hint = hintValue
            inputType = inputTypeValue
            setTextColor(Color.rgb(29, 29, 31))
            setHintTextColor(Color.rgb(134, 134, 139))
            setPadding(dp(16), 0, dp(16), 0)
            background = rounded(Color.WHITE, 14f, Color.rgb(210, 210, 215))
        }
        root.addView(
            view,
            LinearLayout.LayoutParams(
                LinearLayout.LayoutParams.MATCH_PARENT,
                dp(54),
            ).apply { bottomMargin = dp(12) },
        )
        return view
    }

    private fun button(label: String, primary: Boolean, action: () -> Unit) {
        root.addView(
            Button(this).apply {
                text = label
                isAllCaps = false
                textSize = 15f
                typeface = Typeface.DEFAULT_BOLD
                setTextColor(if (primary) Color.WHITE else Color.rgb(0, 113, 227))
                background = rounded(
                    if (primary) Color.rgb(0, 113, 227) else Color.WHITE,
                    28f,
                    if (primary) Color.TRANSPARENT else Color.rgb(210, 210, 215),
                )
                setOnClickListener { action() }
            },
            LinearLayout.LayoutParams(
                LinearLayout.LayoutParams.MATCH_PARENT,
                dp(52),
            ).apply { bottomMargin = dp(12) },
        )
    }

    private fun card(content: LinearLayout.() -> Unit) {
        val card = LinearLayout(this).apply {
            orientation = LinearLayout.VERTICAL
            setPadding(dp(20), dp(10), dp(20), dp(10))
            background = rounded(Color.WHITE, 24f, Color.TRANSPARENT)
            content()
        }
        root.addView(
            card,
            LinearLayout.LayoutParams(
                LinearLayout.LayoutParams.MATCH_PARENT,
                LinearLayout.LayoutParams.WRAP_CONTENT,
            ).apply { bottomMargin = dp(20) },
        )
    }

    private fun LinearLayout.rowLabel(label: String, value: String, good: Boolean) {
        addView(LinearLayout(context).apply {
            gravity = Gravity.CENTER_VERTICAL
            setPadding(0, dp(13), 0, dp(13))
            addView(TextView(context).apply {
                text = label
                textSize = 15f
                setTextColor(Color.rgb(29, 29, 31))
            }, LinearLayout.LayoutParams(0, LinearLayout.LayoutParams.WRAP_CONTENT, 1f))
            addView(TextView(context).apply {
                text = value
                textSize = 13f
                typeface = Typeface.DEFAULT_BOLD
                setTextColor(if (good) Color.rgb(36, 138, 61) else Color.rgb(134, 134, 139))
            })
        })
    }

    private fun sectionTitle(value: String) {
        root.addView(TextView(this).apply {
            text = value
            textSize = 28f
            typeface = Typeface.DEFAULT_BOLD
            setTextColor(Color.rgb(29, 29, 31))
            setPadding(0, dp(26), 0, dp(12))
        })
    }

    private fun feature(number: String, heading: String, copy: String) {
        val wrap = LinearLayout(this).apply {
            orientation = LinearLayout.HORIZONTAL
            gravity = Gravity.TOP
            setPadding(dp(18), dp(18), dp(18), dp(18))
            background = rounded(Color.WHITE, 22f, Color.TRANSPARENT)
        }
        wrap.addView(TextView(this).apply {
            text = number
            gravity = Gravity.CENTER
            textSize = 13f
            typeface = Typeface.DEFAULT_BOLD
            setTextColor(Color.WHITE)
            background = rounded(Color.rgb(29, 29, 31), 20f, Color.TRANSPARENT)
        }, LinearLayout.LayoutParams(dp(40), dp(40)).apply { marginEnd = dp(14) })
        wrap.addView(LinearLayout(this).apply {
            orientation = LinearLayout.VERTICAL
            addView(TextView(this@MainActivity).apply {
                text = heading
                textSize = 16f
                typeface = Typeface.DEFAULT_BOLD
                setTextColor(Color.rgb(29, 29, 31))
            })
            addView(TextView(this@MainActivity).apply {
                text = copy
                textSize = 13f
                setLineSpacing(dp(2).toFloat(), 1f)
                setTextColor(Color.rgb(110, 110, 115))
                setPadding(0, dp(4), 0, 0)
            })
        }, LinearLayout.LayoutParams(0, LinearLayout.LayoutParams.WRAP_CONTENT, 1f))
        root.addView(
            wrap,
            LinearLayout.LayoutParams(
                LinearLayout.LayoutParams.MATCH_PARENT,
                LinearLayout.LayoutParams.WRAP_CONTENT,
            ).apply { bottomMargin = dp(10) },
        )
    }

    private fun statusCard() {
        if (statusMessage.isBlank()) return
        root.addView(TextView(this).apply {
            text = statusMessage
            textSize = 13f
            setTextColor(Color.rgb(29, 29, 31))
            setPadding(dp(16), dp(14), dp(16), dp(14))
            background = rounded(Color.rgb(229, 241, 255), 16f, Color.TRANSPARENT)
        })
    }

    private fun rounded(fill: Int, radiusDp: Float, stroke: Int): GradientDrawable =
        GradientDrawable().apply {
            shape = GradientDrawable.RECTANGLE
            cornerRadius = dp(radiusDp.toInt()).toFloat()
            setColor(fill)
            if (stroke != Color.TRANSPARENT) setStroke(dp(1), stroke)
        }

    private fun dp(value: Int): Int =
        (value * resources.displayMetrics.density).toInt()
}
