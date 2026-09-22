package com.shvya.callintelligence

import android.Manifest
import android.content.Intent
import android.content.pm.PackageManager
import android.graphics.Color
import android.graphics.Typeface
import android.graphics.drawable.GradientDrawable
import android.net.Uri
import android.os.Build
import android.os.Bundle
import android.os.PowerManager
import android.provider.Settings
import android.text.InputType
import android.view.Gravity
import android.view.View
import android.widget.Button
import android.widget.EditText
import android.widget.LinearLayout
import android.widget.ScrollView
import android.widget.TextView
import androidx.activity.result.contract.ActivityResultContracts
import androidx.appcompat.app.AppCompatActivity
import androidx.core.content.ContextCompat
import androidx.lifecycle.lifecycleScope
import com.shvya.callintelligence.calls.CallTrackingService
import com.shvya.callintelligence.calls.TrackingScheduler
import com.shvya.callintelligence.data.AppDatabase
import com.shvya.callintelligence.net.ApiClient
import com.shvya.callintelligence.net.AuthStore
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.launch
import kotlinx.coroutines.withContext

class MainActivity : AppCompatActivity() {
    private lateinit var auth: AuthStore

    private val permissionLauncher =
        registerForActivityResult(ActivityResultContracts.RequestMultiplePermissions()) {
            render()
            if (essentialPermissionsGranted()) {
                TrackingScheduler.activate(this)
            }
        }

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        auth = AuthStore(this)
        render()
    }

    private fun render() {
        if (auth.hasSession()) showDashboard() else showLogin()
    }

    private fun showLogin() {
        val root = page()
        root.addView(kicker("SHVYA CALL INTELLIGENCE"))
        root.addView(title("Your calls.\nYour CRM. Connected."))
        root.addView(body("Sign in with your SHVYA employee account to securely connect this Android phone to your organization."))

        val card = card()
        val email = field("Work email")
        email.inputType = InputType.TYPE_TEXT_VARIATION_EMAIL_ADDRESS
        email.setText(auth.email)
        val password = field("Password")
        password.inputType = InputType.TYPE_CLASS_TEXT or InputType.TYPE_TEXT_VARIATION_PASSWORD
        val message = body("")
        val login = primaryButton("Sign in and connect")
        login.setOnClickListener {
            val emailValue = email.text.toString().trim()
            val passwordValue = password.text.toString()
            if (emailValue.isBlank() || passwordValue.isBlank()) {
                message.text = "Enter your SHVYA email and password."
                message.setTextColor(Color.rgb(210, 45, 40))
                return@setOnClickListener
            }
            login.isEnabled = false
            login.text = "Connecting…"
            lifecycleScope.launch {
                val ok = withContext(Dispatchers.IO) {
                    try {
                        ApiClient(this@MainActivity).login(emailValue, passwordValue)
                    } catch (_: Exception) {
                        false
                    }
                }
                if (ok) {
                    render()
                } else {
                    login.isEnabled = true
                    login.text = "Sign in and connect"
                    message.text = "Could not sign in. Check your credentials and connection."
                    message.setTextColor(Color.rgb(210, 45, 40))
                }
            }
        }
        card.addView(email)
        card.addView(space(10))
        card.addView(password)
        card.addView(space(16))
        card.addView(login)
        card.addView(message)
        root.addView(space(24))
        root.addView(card)
        setPage(root)
    }

    private fun showDashboard() {
        val root = page()
        root.addView(kicker("CONNECTED TO SHVYA"))
        root.addView(title("Call Intelligence"))
        root.addView(body("Use your normal Android SIM dialer. SHVYA will reconcile completed calls and sync them to CRM."))

        val health = card()
        health.addView(sectionTitle("Phone permissions"))
        health.addView(statusLine("Phone state", permissionGranted(Manifest.permission.READ_PHONE_STATE)))
        health.addView(statusLine("Call log", permissionGranted(Manifest.permission.READ_CALL_LOG)))
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.O) {
            health.addView(statusLine("Phone numbers", permissionGranted(Manifest.permission.READ_PHONE_NUMBERS)))
        }
        health.addView(statusLine("Contacts", permissionGranted(Manifest.permission.READ_CONTACTS)))
        if (Build.VERSION.SDK_INT >= 33) {
            health.addView(statusLine("Notifications", permissionGranted(Manifest.permission.POST_NOTIFICATIONS)))
        }
        root.addView(space(22))
        root.addView(health)

        val activate = primaryButton(
            if (essentialPermissionsGranted()) "Call Intelligence is enabled" else "Allow permissions & enable"
        )
        activate.setOnClickListener {
            if (essentialPermissionsGranted()) {
                TrackingScheduler.activate(this)
                activate.text = "Call Intelligence is enabled"
            } else {
                permissionLauncher.launch(requiredPermissions())
            }
        }
        root.addView(space(14))
        root.addView(activate)

        val battery = secondaryButton("Allow unrestricted background use")
        battery.setOnClickListener { requestBatteryOptimizationExemption() }
        root.addView(space(10))
        root.addView(battery)

        val workspace = secondaryButton("Open SHVYA Call Intelligence")
        workspace.setOnClickListener {
            startActivity(
                Intent(
                    Intent.ACTION_VIEW,
                    Uri.parse(BuildConfig.SHVYA_BASE_URL.trimEnd('/') + "/dashboard/call-intelligence/")
                )
            )
        }
        root.addView(space(10))
        root.addView(workspace)

        val syncNow = secondaryButton("Sync calls now")
        syncNow.setOnClickListener {
            TrackingScheduler.enqueueReconcile(this)
            TrackingScheduler.enqueueSync(this)
            syncNow.text = "Sync requested ✓"
        }
        root.addView(space(10))
        root.addView(syncNow)

        val queueCard = card()
        queueCard.addView(sectionTitle("Offline-safe outbox"))
        val pendingText = body("Checking pending calls…")
        queueCard.addView(pendingText)
        root.addView(space(22))
        root.addView(queueCard)
        lifecycleScope.launch {
            val count = withContext(Dispatchers.IO) {
                AppDatabase.get(this@MainActivity).callDao().pendingCount()
            }
            pendingText.text = if (count == 0) {
                "Everything is synced."
            } else {
                count.toString() + " call(s) waiting to sync. They stay on this device until SHVYA confirms receipt."
            }
        }

        val privacy = card()
        privacy.addView(sectionTitle("How it works"))
        privacy.addView(body("1. Android detects the phone-state change.\n2. The call log confirms the final SIM call.\n3. Room stores it locally first.\n4. SHVYA matches or creates the CRM lead.\n5. Notes, reminders and AI intelligence continue in the dashboard."))
        root.addView(space(22))
        root.addView(privacy)

        val logout = secondaryButton("Disconnect this employee")
        logout.setOnClickListener {
            stopService(Intent(this, CallTrackingService::class.java))
            auth.clear()
            render()
        }
        root.addView(space(24))
        root.addView(logout)
        root.addView(space(30))
        setPage(root)

        if (essentialPermissionsGranted()) {
            TrackingScheduler.ensureBaseline(this)
            TrackingScheduler.scheduleRecurring(this)
        }
    }

    private fun essentialPermissionsGranted(): Boolean =
        permissionGranted(Manifest.permission.READ_PHONE_STATE) &&
            permissionGranted(Manifest.permission.READ_CALL_LOG) &&
            permissionGranted(Manifest.permission.READ_CONTACTS) &&
            (
                Build.VERSION.SDK_INT < Build.VERSION_CODES.O ||
                    permissionGranted(Manifest.permission.READ_PHONE_NUMBERS)
            )

    private fun requiredPermissions(): Array<String> {
        val result = mutableListOf(
            Manifest.permission.READ_PHONE_STATE,
            Manifest.permission.READ_CALL_LOG,
            Manifest.permission.READ_CONTACTS,
        )
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.O) {
            result += Manifest.permission.READ_PHONE_NUMBERS
        }
        if (Build.VERSION.SDK_INT >= 33) {
            result += Manifest.permission.POST_NOTIFICATIONS
        }
        return result.toTypedArray()
    }

    private fun permissionGranted(permission: String): Boolean =
        ContextCompat.checkSelfPermission(this, permission) == PackageManager.PERMISSION_GRANTED

    private fun requestBatteryOptimizationExemption() {
        val power = getSystemService(POWER_SERVICE) as PowerManager
        if (power.isIgnoringBatteryOptimizations(packageName)) return
        try {
            startActivity(
                Intent(Settings.ACTION_REQUEST_IGNORE_BATTERY_OPTIMIZATIONS).apply {
                    data = Uri.parse("package:" + packageName)
                }
            )
        } catch (_: Exception) {
            startActivity(Intent(Settings.ACTION_IGNORE_BATTERY_OPTIMIZATION_SETTINGS))
        }
    }

    private fun page(): LinearLayout =
        LinearLayout(this).apply {
            orientation = LinearLayout.VERTICAL
            setPadding(dp(24), dp(48), dp(24), dp(28))
            setBackgroundColor(Color.rgb(245, 245, 247))
        }

    private fun setPage(content: LinearLayout) {
        val scroll = ScrollView(this)
        scroll.addView(content)
        setContentView(scroll)
    }

    private fun card(): LinearLayout =
        LinearLayout(this).apply {
            orientation = LinearLayout.VERTICAL
            setPadding(dp(20), dp(20), dp(20), dp(20))
            background = rounded(Color.WHITE, 24f)
        }

    private fun kicker(text: String): TextView =
        TextView(this).apply {
            this.text = text
            textSize = 11f
            setTextColor(Color.rgb(105, 105, 110))
            setTypeface(typeface, Typeface.BOLD)
            letterSpacing = .12f
        }

    private fun title(text: String): TextView =
        TextView(this).apply {
            this.text = text
            textSize = 38f
            setTextColor(Color.rgb(29, 29, 31))
            setTypeface(typeface, Typeface.BOLD)
            setLineSpacing(0f, .94f)
            setPadding(0, dp(12), 0, dp(14))
        }

    private fun sectionTitle(text: String): TextView =
        TextView(this).apply {
            this.text = text
            textSize = 17f
            setTextColor(Color.rgb(29, 29, 31))
            setTypeface(typeface, Typeface.BOLD)
            setPadding(0, 0, 0, dp(12))
        }

    private fun body(text: String): TextView =
        TextView(this).apply {
            this.text = text
            textSize = 14f
            setTextColor(Color.rgb(110, 110, 115))
            setLineSpacing(dp(3).toFloat(), 1f)
            setPadding(0, dp(6), 0, 0)
        }

    private fun statusLine(label: String, ok: Boolean): TextView =
        TextView(this).apply {
            text = (if (ok) "✓  " else "○  ") + label
            textSize = 13f
            setTextColor(if (ok) Color.rgb(38, 132, 53) else Color.rgb(125, 125, 130))
            setPadding(0, dp(7), 0, dp(7))
        }

    private fun field(hintText: String): EditText =
        EditText(this).apply {
            hint = hintText
            textSize = 15f
            setTextColor(Color.rgb(29, 29, 31))
            setHintTextColor(Color.rgb(145, 145, 150))
            setPadding(dp(15), 0, dp(15), 0)
            minHeight = dp(52)
            background = rounded(Color.rgb(245, 245, 247), 15f)
        }

    private fun primaryButton(label: String): Button =
        Button(this).apply {
            text = label
            isAllCaps = false
            textSize = 14f
            setTextColor(Color.WHITE)
            minHeight = dp(52)
            background = rounded(Color.rgb(0, 113, 227), 26f)
        }

    private fun secondaryButton(label: String): Button =
        Button(this).apply {
            text = label
            isAllCaps = false
            textSize = 14f
            setTextColor(Color.rgb(29, 29, 31))
            minHeight = dp(50)
            background = rounded(Color.WHITE, 25f)
        }

    private fun rounded(color: Int, radius: Float): GradientDrawable =
        GradientDrawable().apply {
            setColor(color)
            cornerRadius = dp(radius.toInt()).toFloat()
        }

    private fun space(height: Int): View =
        View(this).apply {
            layoutParams = LinearLayout.LayoutParams(1, dp(height))
        }

    private fun dp(value: Int): Int =
        (value * resources.displayMetrics.density).toInt()
}
