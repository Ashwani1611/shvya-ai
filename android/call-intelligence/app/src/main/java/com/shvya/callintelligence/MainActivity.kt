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
import android.widget.ImageView
import android.widget.Switch
import android.app.DatePickerDialog
import android.app.AlertDialog
import android.widget.Toast
import androidx.core.view.ViewCompat
import androidx.core.view.WindowInsetsCompat
import androidx.work.WorkManager
import org.json.JSONObject
import java.util.Calendar
import java.util.Locale
import java.text.SimpleDateFormat
import java.net.URLEncoder
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
import com.shvya.callintelligence.net.ApiClient
import com.shvya.callintelligence.net.AuthStore
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.launch
import kotlinx.coroutines.withContext

class MainActivity : AppCompatActivity() {
    private lateinit var auth: AuthStore
    private var selectedTab = "home"
    private var query = ""
    private var dateFrom = ""
    private var dateTo = ""
    private var pageNumber = 1
    private var renderVersion = 0
    private val apiPath = "api/v1/call-intelligence/"

    override fun onResume() {
        super.onResume()
        if (::auth.isInitialized && auth.hasSession()) render()
    }

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
        root.addView(ImageView(this).apply {
            setImageResource(R.drawable.ic_shvya)
            contentDescription = "SHVYA"
            layoutParams = LinearLayout.LayoutParams(dp(64), dp(64))
        })
        root.addView(space(20))
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
        val version = ++renderVersion
        val root = page()
        val header = LinearLayout(this).apply { gravity = Gravity.CENTER_VERTICAL }
        header.addView(ImageView(this).apply {
            setImageResource(R.drawable.ic_shvya)
            contentDescription = "SHVYA"
        }, LinearLayout.LayoutParams(dp(46), dp(46)))
        header.addView(sectionTitle("Call Intelligence").apply { setPadding(dp(12), 0, 0, 0); textSize = 20f },
            LinearLayout.LayoutParams(0, -2, 1f))
        header.addView(secondaryButton("Settings").apply { setOnClickListener { showSettings() } })
        root.addView(header)
        root.addView(kicker("SHVYA · YOUR CALL WORKSPACE"))
        root.addView(space(24))

        if (!essentialPermissionsGranted()) {
            val permissionCard = card()
            permissionCard.addView(sectionTitle("Permissions required"))
            permissionCard.addView(body("Allow phone and call log access to capture your calls. Contacts help identify callers; notifications keep you informed."))
            permissionCard.addView(space(20))
            permissionCard.addView(primaryButton("Allow access").apply {
                setOnClickListener { permissionLauncher.launch(requiredPermissions()) }
            })
            permissionCard.addView(secondaryButton("Open app permissions").apply {
                setOnClickListener { startActivity(Intent(Settings.ACTION_APPLICATION_DETAILS_SETTINGS, Uri.parse("package:$packageName"))) }
            })
            root.addView(permissionCard)
        } else if (selectedTab == "reminders") {
            showReminders(root, version)
        } else {
            showCalls(root, version)
        }
        setDashboardPage(root)
        if (essentialPermissionsGranted()) {
            TrackingScheduler.ensureBaseline(this)
            TrackingScheduler.scheduleRecurring(this)
        }
    }

    private fun showCalls(root: LinearLayout, version: Int) {
        val search = field("Search lead or phone").apply {
            setText(query)
            isSingleLine = true
            imeOptions = android.view.inputmethod.EditorInfo.IME_ACTION_SEARCH
        }
        root.addView(kicker("CALL ACTIVITY")); root.addView(space(10)); root.addView(search)
        val actions = LinearLayout(this)
        actions.addView(secondaryButton("Search").apply { setOnClickListener {
            query = search.text.toString().trim(); pageNumber = 1; render()
        } }, LinearLayout.LayoutParams(0, -2, 1f))
        actions.addView(primaryButton("+ Add lead").apply { setOnClickListener { addLead() } },
            LinearLayout.LayoutParams(0, -2, 1f))
        root.addView(space(10)); root.addView(actions)
        search.setOnEditorActionListener { _, _, _ ->
            query = search.text.toString().trim(); pageNumber = 1; render(); true
        }
        val filters = LinearLayout(this)
        filters.addView(secondaryButton(if (dateFrom.isBlank()) "All dates" else "$dateFrom – $dateTo").apply {
            setOnClickListener { pickDate("From date") { start ->
                pickDate("To date") { end ->
                    if (end < start) toast("End date must follow the start date.") else {
                        dateFrom = start; dateTo = end; pageNumber = 1; render()
                    }
                }
            } }
        }, LinearLayout.LayoutParams(0, -2, 1f))
        filters.addView(secondaryButton("Clear").apply { setOnClickListener {
            query = ""; dateFrom = ""; dateTo = ""; pageNumber = 1; render()
        } })
        root.addView(filters); root.addView(space(18))
        val content = LinearLayout(this).apply { orientation = LinearLayout.VERTICAL }
        root.addView(content)
        val path = apiPath + "calls/?mine=1&page=$pageNumber&q=" + URLEncoder.encode(query, "UTF-8") +
            "&date_from=$dateFrom&date_to=$dateTo"
        loadInto(content, version, { ApiClient(this).authorizedGet(path) }) { data ->
            val stats = data.getJSONObject("stats")
            val summary = card()
            summary.addView(kicker("AT A GLANCE")); summary.addView(space(10)); summary.addView(sectionTitle("Call statistics"))
            summary.addView(metricRow(listOf("Total" to stats.optInt("total"), "Incoming" to stats.optInt("incoming"), "Missed" to stats.optInt("missed"))))
            summary.addView(metricRow(listOf("Outgoing" to stats.optInt("outgoing"), "Picked" to stats.optInt("picked"), "Not picked" to stats.optInt("not_picked"))))
            summary.addView(body("Measured ringing time: " + stats.optInt("total_ring") + "s"))
            content.addView(summary); content.addView(space(20)); content.addView(sectionTitle("Recent calls"))
            val calls = data.getJSONArray("calls")
            if (calls.length() == 0) {
                content.addView(emptyCard("No calls found", "Incoming, outgoing and missed calls appear here after sync. Try clearing your filters."))
            }
            for (i in 0 until calls.length()) {
                val call = calls.getJSONObject(i)
                val row = card()
                val lead = call.optJSONObject("lead")
                val name = call.optString("contact_name").ifBlank { lead?.optString("name").orEmpty() }
                row.addView(sectionTitle(name.ifBlank { call.optString("phone_number") }))
                row.addView(body(call.optString("phone_number") + " · " + call.optString("direction") + " · " + call.optString("status").replace('_', ' ')))
                row.addView(body(formatDate(call.optString("ended_at"))))
                val ring = call.optInt("ring_duration_seconds")
                row.addView(body("Talk: " + call.optInt("talk_duration_seconds") + "s  ·  Rang: " + if (ring > 0) "${ring}s" else "Not available"))
                row.addView(secondaryButton("Call lead").apply { setOnClickListener { dial(call.optString("phone_number")) } })
                if (call.optString("notes").isNotBlank()) row.addView(body(call.optString("notes")))
                row.addView(secondaryButton(if (call.optString("notes").isBlank()) "Add call notes" else "Edit call notes").apply {
                    setOnClickListener { editCallNotes(call) }
                })
                content.addView(row); content.addView(space(12))
            }
            content.addView(pagination(data.optBoolean("has_next")))
            content.addView(body("Ring time is the measured duration, not a count of audible rings. Some calls do not supply it."))
        }
        root.addView(space(12))
        root.addView(secondaryButton("Sync calls now").apply { setOnClickListener {
            TrackingScheduler.enqueueReconcile(this@MainActivity)
            TrackingScheduler.enqueueSync(this@MainActivity)
            toast("Sync requested. Refresh to see new calls.")
        } })
        root.addView(secondaryButton("Refresh").apply { setOnClickListener { render() } })
    }

    private fun showReminders(root: LinearLayout, version: Int) {
        val content = LinearLayout(this).apply { orientation = LinearLayout.VERTICAL }
        root.addView(content)
        loadInto(content, version, { ApiClient(this).authorizedGet(apiPath + "reminders/?page=$pageNumber") }) { data ->
            val stats = data.getJSONObject("stats")
            val summary = card()
            summary.addView(kicker("YOUR CRM FOLLOW-UPS")); summary.addView(space(10)); summary.addView(sectionTitle("Call reminders"))
            summary.addView(metricRow(listOf("Total" to stats.optInt("total"), "Upcoming" to stats.optInt("upcoming"))))
            summary.addView(metricRow(listOf("Overdue" to stats.optInt("overdue"), "Today" to stats.optInt("today"))))
            summary.addView(body("Today includes reminders already overdue today."))
            content.addView(summary); content.addView(space(24))
            content.addView(sectionTitle("Reminder list"))
            val rows = data.getJSONArray("reminders")
            if (rows.length() == 0) content.addView(emptyCard("You're all caught up", "Your assigned CRM reminders will appear here."))
            for (i in 0 until rows.length()) {
                val item = rows.getJSONObject(i)
                val row = card()
                if (item.optBoolean("overdue")) row.background = rounded(Color.rgb(255, 246, 245), 22f)
                row.addView(sectionTitle(item.optString("lead_name")))
                row.addView(body(formatDate(item.optString("due_at"))))
                row.addView(body(item.optString("title")))
                if (item.optString("description").isNotBlank()) row.addView(body("Note: " + item.optString("description")))
                val controls = LinearLayout(this)
                controls.addView(secondaryButton("Snooze 30m").apply { setOnClickListener { reminderAction(item, "snooze", this) } }, LinearLayout.LayoutParams(0, -2, 1f))
                controls.addView(primaryButton("Mark done").apply { setOnClickListener { reminderAction(item, "complete", this) } }, LinearLayout.LayoutParams(0, -2, 1f))
                row.addView(space(12)); row.addView(controls)
                row.addView(secondaryButton("Call lead").apply { setOnClickListener { dial(item.optString("phone")) } })
                row.addView(secondaryButton("Delete reminder").apply { setOnClickListener {
                    AlertDialog.Builder(this@MainActivity).setTitle("Delete reminder?")
                        .setMessage(item.optString("lead_name")).setNegativeButton("Cancel", null)
                        .setPositiveButton("Delete") { _, _ -> reminderAction(item, "delete", this) }.show()
                } })
                content.addView(row); content.addView(space(14))
            }
            content.addView(pagination(data.optBoolean("has_next")))
        }
    }

    private fun reminderAction(item: JSONObject, action: String, button: Button) {
        button.isEnabled = false
        lifecycleScope.launch {
            val response = withContext(Dispatchers.IO) {
                runCatching { ApiClient(this@MainActivity).authorizedPost(apiPath + "reminders/" + item.getString("id") + "/action/", JSONObject().put("action", action)) }.getOrNull()
            }
            if (response?.successful == true) render() else {
                button.isEnabled = true; toast("Could not update the reminder. Please retry.")
            }
        }
    }

    private fun editCallNotes(call: JSONObject) {
        val content = card()
        val notes = field("What happened on the call?").apply {
            setText(call.optString("notes"))
            isSingleLine = false
            minLines = 5
            gravity = Gravity.TOP
            setPadding(dp(16), dp(14), dp(16), dp(14))
            inputType = InputType.TYPE_CLASS_TEXT or InputType.TYPE_TEXT_FLAG_MULTI_LINE or InputType.TYPE_TEXT_FLAG_CAP_SENTENCES
        }
        val message = body("Notes are saved to this call and its CRM activity.")
        content.addView(notes); content.addView(space(8)); content.addView(message)
        val dialog = AlertDialog.Builder(this).setTitle("Call notes").setView(content)
            .setNegativeButton("Cancel", null).setPositiveButton("Save notes", null).create()
        dialog.setOnShowListener {
            val button = dialog.getButton(AlertDialog.BUTTON_POSITIVE)
            button.setOnClickListener {
                button.isEnabled = false
                lifecycleScope.launch {
                    val result = withContext(Dispatchers.IO) {
                        runCatching {
                            ApiClient(this@MainActivity).authorizedPatch(
                                apiPath + "calls/" + call.getString("id") + "/notes/",
                                JSONObject().put("notes", notes.text.toString().trim()),
                            )
                        }.getOrNull()
                    }
                    if (result?.successful == true) { dialog.dismiss(); toast("Call notes saved to CRM"); render() }
                    else { button.isEnabled = true; message.text = "Could not save notes. Please retry." }
                }
            }
        }
        dialog.show()
    }

    private fun showSettings() {
        val content = card()
        val scroll = ScrollView(this).apply { addView(content) }
        val dialog = AlertDialog.Builder(this).setTitle("Settings").setView(scroll).setNegativeButton("Close", null).create()
        content.addView(body("Loading lead creation settings…"))
        dialog.show()
        lifecycleScope.launch {
            val response = withContext(Dispatchers.IO) { runCatching { ApiClient(this@MainActivity).authorizedGet(apiPath + "settings/") }.getOrNull() }
            content.removeAllViews()
            if (response?.successful == true) {
                val data = JSONObject(response.body)
                val canEdit = data.optBoolean("can_edit")
                val incoming = data.optBoolean("auto_create_answered_incoming")
                val outgoing = data.optBoolean("auto_create_answered_outgoing")
                val answered = Switch(this@MainActivity).apply {
                    text = "Auto create lead · answered calls"
                    isChecked = incoming && outgoing
                    isEnabled = canEdit
                    minHeight = dp(56)
                }
                val missed = Switch(this@MainActivity).apply {
                    text = "Auto create lead · missed calls"
                    isChecked = data.optBoolean("auto_create_missed")
                    isEnabled = canEdit
                    minHeight = dp(56)
                }
                content.addView(answered)
                if (incoming != outgoing) content.addView(body("Incoming and outgoing rules differ. Changing this switch updates both."))
                content.addView(missed)
                content.addView(body(if (canEdit) "These settings apply to your organization." else "Your administrator manages lead creation settings."))
                var answeredChanged = false
                var missedChanged = false
                answered.setOnCheckedChangeListener { _, _ -> answeredChanged = true }
                missed.setOnCheckedChangeListener { _, _ -> missedChanged = true }
                if (canEdit) content.addView(primaryButton("Save settings").apply { setOnClickListener {
                    val changes = JSONObject()
                    if (answeredChanged) changes.put("auto_create_answered_incoming", answered.isChecked).put("auto_create_answered_outgoing", answered.isChecked)
                    if (missedChanged) changes.put("auto_create_missed", missed.isChecked)
                    if (changes.length() == 0) { dialog.dismiss(); return@setOnClickListener }
                    isEnabled = false
                    lifecycleScope.launch {
                        val result = withContext(Dispatchers.IO) { runCatching { ApiClient(this@MainActivity).authorizedPatch(apiPath + "settings/", changes) }.getOrNull() }
                        if (result?.successful == true) { toast("Settings saved"); dialog.dismiss() }
                        else { isEnabled = true; toast("Could not save settings. Please retry.") }
                    }
                } })
            } else content.addView(body("Could not load settings. Close and retry when connected."))
            content.addView(secondaryButton("Phone permissions").apply { setOnClickListener { permissionLauncher.launch(requiredPermissions()) } })
            content.addView(secondaryButton("Background access").apply { setOnClickListener { requestBatteryOptimizationExemption() } })
            content.addView(secondaryButton("Open dashboard").apply { setOnClickListener {
                startActivity(Intent(Intent.ACTION_VIEW, Uri.parse(BuildConfig.SHVYA_BASE_URL.trimEnd('/') + "/dashboard/call-intelligence/?section=analytics")))
            } })
            content.addView(body("Version " + BuildConfig.VERSION_NAME + " (" + BuildConfig.VERSION_CODE + ")"))
            content.addView(secondaryButton("Sign out").apply { setOnClickListener {
                AlertDialog.Builder(this@MainActivity).setTitle("Sign out?")
                    .setMessage("Call tracking will stop. Sync any pending calls first.")
                    .setNegativeButton("Cancel", null).setPositiveButton("Sign out") { _, _ ->
                        stopService(Intent(this@MainActivity, CallTrackingService::class.java))
                        WorkManager.getInstance(this@MainActivity).cancelAllWork()
                        auth.clear(); dialog.dismiss(); render()
                    }.show()
            } })
        }
    }

    private fun addLead() {
        val content = card()
        val name = field("Lead name")
        val phone = field("Phone with country code").apply { inputType = InputType.TYPE_CLASS_PHONE }
        val message = body("")
        content.addView(name); content.addView(space(12)); content.addView(phone); content.addView(message)
        val dialog = AlertDialog.Builder(this).setTitle("Add lead").setView(content)
            .setNegativeButton("Cancel", null).setPositiveButton("Create", null).create()
        dialog.setOnShowListener {
            val button = dialog.getButton(AlertDialog.BUTTON_POSITIVE)
            button.setOnClickListener {
                if (name.text.isBlank() || phone.text.isBlank()) { message.text = "Enter a name and phone number."; return@setOnClickListener }
                button.isEnabled = false
                lifecycleScope.launch {
                    val result = withContext(Dispatchers.IO) { runCatching { ApiClient(this@MainActivity).authorizedPost(apiPath + "leads/", JSONObject().put("name", name.text.toString()).put("phone", phone.text.toString())) }.getOrNull() }
                    if (result?.successful == true) { dialog.dismiss(); toast("Lead added to CRM") } else {
                        button.isEnabled = true
                        message.text = if (result?.code == 409) "This number is already in your CRM." else "Could not create lead. Check the phone number and connection."
                    }
                }
            }
        }
        dialog.show()
    }

    private fun loadInto(content: LinearLayout, version: Int, request: () -> com.shvya.callintelligence.net.ApiResponse, loaded: (JSONObject) -> Unit) {
        content.addView(body("Loading…"))
        lifecycleScope.launch {
            val response = withContext(Dispatchers.IO) { runCatching { request() }.getOrNull() }
            if (version != renderVersion || isFinishing) return@launch
            content.removeAllViews()
            val data = if (response?.successful == true) runCatching { JSONObject(response.body) }.getOrNull() else null
            if (data != null && runCatching { loaded(data) }.isSuccess) return@launch
            else {
                content.removeAllViews()
                content.addView(emptyCard("Unable to load", "Check your connection and retry. Your captured calls remain queued for sync."))
                content.addView(secondaryButton("Retry").apply { setOnClickListener { render() } })
            }
        }
    }

    private fun metricRow(values: List<Pair<String, Int>>): LinearLayout = LinearLayout(this).apply {
        values.forEach { (label, value) ->
            addView(LinearLayout(this@MainActivity).apply {
                orientation = LinearLayout.VERTICAL
                gravity = Gravity.CENTER
                setPadding(dp(4), dp(12), dp(4), dp(12))
                addView(sectionTitle(value.toString()).apply { textSize = 28f; setTextColor(Color.rgb(0, 113, 227)) })
                addView(body(label).apply { textSize = 12f; gravity = Gravity.CENTER })
            }, LinearLayout.LayoutParams(0, -2, 1f))
        }
    }

    private fun pagination(hasNext: Boolean): LinearLayout = LinearLayout(this).apply {
        if (pageNumber > 1) addView(secondaryButton("Previous").apply { setOnClickListener { pageNumber--; render() } })
        addView(body("Page $pageNumber"), LinearLayout.LayoutParams(0, -2, 1f))
        if (hasNext) addView(secondaryButton("Next").apply { setOnClickListener { pageNumber++; render() } })
    }

    private fun emptyCard(heading: String, text: String): LinearLayout = card().apply {
        addView(sectionTitle(heading)); addView(body(text))
    }

    private fun setDashboardPage(content: LinearLayout) {
        val shell = LinearLayout(this).apply {
            orientation = LinearLayout.VERTICAL
            setBackgroundColor(Color.rgb(245, 245, 247))
        }
        shell.addView(ScrollView(this).apply { addView(content) }, LinearLayout.LayoutParams(-1, 0, 1f))
        val nav = LinearLayout(this).apply { setPadding(dp(16), dp(8), dp(16), dp(8)); setBackgroundColor(Color.WHITE) }
        listOf("home" to "Home", "reminders" to "Reminders").forEach { (key, label) ->
            val button = if (selectedTab == key) primaryButton(label) else secondaryButton(label)
            button.isSelected = selectedTab == key
            button.setOnClickListener { selectedTab = key; pageNumber = 1; render() }
            nav.addView(button, LinearLayout.LayoutParams(0, -2, 1f).apply { setMargins(dp(4), 0, dp(4), 0) })
        }
        shell.addView(nav)
        ViewCompat.setOnApplyWindowInsetsListener(shell) { view, insets ->
            val bars = insets.getInsets(WindowInsetsCompat.Type.systemBars())
            view.setPadding(bars.left, bars.top, bars.right, bars.bottom)
            insets
        }
        setContentView(shell)
    }

    private fun pickDate(label: String, chosen: (String) -> Unit) {
        val now = Calendar.getInstance()
        DatePickerDialog(this, { _, y, m, d -> chosen(String.format(Locale.US, "%04d-%02d-%02d", y, m + 1, d)) }, now.get(Calendar.YEAR), now.get(Calendar.MONTH), now.get(Calendar.DAY_OF_MONTH)).apply { setTitle(label); show() }
    }

    private fun formatDate(value: String): String = try {
        val parser = SimpleDateFormat("yyyy-MM-dd'T'HH:mm:ssXXX", Locale.US)
        val date = parser.parse(value.replace(Regex("\\.\\d+(?=[Z+-])"), ""))
        if (date == null) value else SimpleDateFormat("d MMM, h:mm a", Locale.getDefault()).format(date)
    } catch (_: Exception) { value }

    private fun dial(phone: String) {
        runCatching { startActivity(Intent(Intent.ACTION_DIAL, Uri.fromParts("tel", phone, null))) }
            .onFailure { toast("No phone app is available.") }
    }

    private fun toast(message: String) = Toast.makeText(this, message, Toast.LENGTH_LONG).show()

    private fun essentialPermissionsGranted(): Boolean =
        permissionGranted(Manifest.permission.READ_PHONE_STATE) &&
            permissionGranted(Manifest.permission.READ_CALL_LOG)

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
            setPadding(dp(20), dp(24), dp(20), dp(28))
            setBackgroundColor(Color.rgb(246, 247, 251))
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
            background = rounded(Color.WHITE, 22f)
            elevation = dp(2).toFloat()
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
