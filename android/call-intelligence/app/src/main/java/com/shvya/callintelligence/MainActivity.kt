package com.shvya.callintelligence

import android.Manifest
import android.content.Intent
import android.content.pm.PackageManager
import android.graphics.Color
import android.graphics.Typeface
import android.graphics.drawable.GradientDrawable
import android.graphics.drawable.RippleDrawable
import android.content.res.ColorStateList
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
    private val ink = Color.rgb(22, 29, 39)
    private val muted = Color.rgb(107, 116, 129)
    private val blue = Color.rgb(25, 94, 185)
    private val canvas = Color.rgb(246, 247, 249)
    private val line = Color.rgb(233, 236, 240)
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
        root.addView(space(54))
        root.addView(ImageView(this).apply {
            setImageResource(R.drawable.ic_shvya)
            contentDescription = "SHVYA"
            layoutParams = LinearLayout.LayoutParams(dp(70), dp(70)).apply { gravity = Gravity.CENTER_HORIZONTAL }
        })
        root.addView(space(26))
        root.addView(kicker("SHVYA CALL INTELLIGENCE").apply { gravity = Gravity.CENTER })
        root.addView(title("Welcome back.").apply { gravity = Gravity.CENTER; textSize = 34f })
        root.addView(body("Your calls and CRM, together in one place.").apply { gravity = Gravity.CENTER })
        root.addView(space(42))

        val card = card()
        card.addView(sectionTitle("Sign in").apply { textSize = 22f })
        card.addView(body("Use your SHVYA work account."))
        card.addView(space(24))
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
        card.addView(fieldLabel("Work email"))
        card.addView(space(8))
        card.addView(email)
        card.addView(space(18))
        card.addView(fieldLabel("Password"))
        card.addView(space(8))
        card.addView(password)
        card.addView(space(24))
        card.addView(login)
        card.addView(message)
        root.addView(card)
        root.addView(space(20))
        root.addView(body("Secure access to your organization's CRM").apply { gravity = Gravity.CENTER })
        setPage(root)
    }

    private fun showDashboard() {
        val version = ++renderVersion
        val root = page()
        val header = LinearLayout(this).apply { gravity = Gravity.CENTER_VERTICAL; minimumHeight = dp(56) }
        header.addView(ImageView(this).apply {
            setImageResource(R.drawable.ic_shvya)
            contentDescription = "SHVYA"
        }, LinearLayout.LayoutParams(dp(42), dp(42)))
        header.addView(LinearLayout(this).apply {
            orientation = LinearLayout.VERTICAL
            setPadding(dp(12), 0, 0, 0)
            addView(kicker("SHVYA"))
            addView(sectionTitle("Call Intelligence").apply { setPadding(0, 0, 0, 0); textSize = 17f })
        },
            LinearLayout.LayoutParams(0, -2, 1f))
        header.addView(iconAction(R.drawable.ic_settings_outline, "Settings").apply { setOnClickListener { showSettings() } })
        root.addView(header)
        root.addView(space(30))

        if (!essentialPermissionsGranted()) {
            val permissionCard = card()
            permissionCard.addView(iconBadge(R.drawable.ic_phone_outline, blue, Color.rgb(234, 242, 253)))
            permissionCard.addView(space(18))
            permissionCard.addView(sectionTitle("Connect your calls").apply { textSize = 23f })
            permissionCard.addView(body("Allow phone and call log access to capture your calls. Contacts help identify callers; notifications keep you informed."))
            permissionCard.addView(space(20))
            permissionCard.addView(primaryButton("Allow access").apply {
                setOnClickListener { permissionLauncher.launch(requiredPermissions()) }
            })
            permissionCard.addView(space(8))
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
        root.addView(kicker("YOUR WORKSPACE"))
        root.addView(title("Calls").apply { textSize = 36f })
        root.addView(body("Every conversation, thoughtfully organized."))
        root.addView(space(26))

        val search = field("Search leads or numbers").apply {
            setText(query)
            isSingleLine = true
            imeOptions = android.view.inputmethod.EditorInfo.IME_ACTION_SEARCH
            setCompoundDrawablesWithIntrinsicBounds(R.drawable.ic_search_outline, 0, 0, 0)
            compoundDrawablePadding = dp(10)
            setOnEditorActionListener { _, _, _ ->
                query = text.toString().trim(); pageNumber = 1; render(); true
            }
        }
        val searchRow = LinearLayout(this).apply { gravity = Gravity.CENTER_VERTICAL }
        searchRow.addView(search, LinearLayout.LayoutParams(0, dp(54), 1f))
        searchRow.addView(iconAction(R.drawable.ic_plus_outline, "Add lead").apply {
            setOnClickListener { addLead() }
        }, LinearLayout.LayoutParams(dp(54), dp(54)).apply { leftMargin = dp(10) })
        root.addView(searchRow)
        root.addView(space(12))
        val filters = LinearLayout(this).apply { gravity = Gravity.CENTER_VERTICAL }
        filters.addView(quietButton(if (dateFrom.isBlank()) "All dates  ⌄" else "$dateFrom – $dateTo").apply {
            setOnClickListener { pickDate("From date") { start ->
                pickDate("To date") { end ->
                    if (end < start) toast("End date must follow the start date.") else {
                        dateFrom = start; dateTo = end; pageNumber = 1; render()
                    }
                }
            } }
        })
        if (query.isNotBlank() || dateFrom.isNotBlank()) filters.addView(quietButton("Clear filters").apply {
            setOnClickListener { query = ""; dateFrom = ""; dateTo = ""; pageNumber = 1; render() }
        })
        root.addView(filters)
        root.addView(space(20))

        val content = LinearLayout(this).apply { orientation = LinearLayout.VERTICAL }
        root.addView(content)
        val path = apiPath + "calls/?mine=1&page=$pageNumber&q=" + URLEncoder.encode(query, "UTF-8") +
            "&date_from=$dateFrom&date_to=$dateTo"
        loadInto(content, version, { ApiClient(this).authorizedGet(path) }) { data ->
            val stats = data.getJSONObject("stats")
            val summary = card()
            summary.addView(sectionTitle("Call activity"))
            summary.addView(body("A clear view of your conversations."))
            summary.addView(space(20))
            summary.addView(metricRow(listOf(
                "Total" to stats.optInt("total"), "Incoming" to stats.optInt("incoming"),
                "Missed" to stats.optInt("missed"), "Outgoing" to stats.optInt("outgoing"),
            )))
            summary.addView(space(18))
            summary.addView(divider())
            summary.addView(space(14))
            val outcomes = LinearLayout(this@MainActivity)
            outcomes.addView(outcome("●  Picked", stats.optInt("picked"), Color.rgb(34, 145, 101)),
                LinearLayout.LayoutParams(0, -2, 1f))
            outcomes.addView(outcome("●  Not picked", stats.optInt("not_picked"), Color.rgb(209, 84, 70)),
                LinearLayout.LayoutParams(0, -2, 1f))
            summary.addView(outcomes)
            content.addView(summary)
            content.addView(space(32))
            content.addView(sectionHeader("Recent calls", "Refresh").apply {
                setOnClickListener { render() }
            })
            content.addView(space(14))
            val calls = data.getJSONArray("calls")
            if (calls.length() == 0) content.addView(emptyCard("No calls yet", "Your synced calls will appear here. Try clearing your filters or syncing your phone."))
            for (i in 0 until calls.length()) {
                val call = calls.getJSONObject(i)
                val row = card()
                val lead = call.optJSONObject("lead")
                val name = call.optString("contact_name").ifBlank { lead?.optString("name").orEmpty() }
                    .ifBlank { call.optString("phone_number") }
                val missed = call.optString("status") == "missed"
                val top = LinearLayout(this@MainActivity).apply { gravity = Gravity.CENTER_VERTICAL }
                top.addView(iconBadge(R.drawable.ic_phone_outline,
                    if (missed) Color.rgb(204, 79, 65) else blue,
                    if (missed) Color.rgb(255, 238, 235) else Color.rgb(236, 244, 255)))
                top.addView(LinearLayout(this@MainActivity).apply {
                    orientation = LinearLayout.VERTICAL
                    addView(sectionTitle(name).apply { setPadding(0, 0, 0, 0); maxLines = 1; ellipsize = android.text.TextUtils.TruncateAt.END })
                    addView(body(call.optString("phone_number")).apply { setPadding(0, dp(3), 0, 0) })
                }, LinearLayout.LayoutParams(0, -2, 1f).apply { leftMargin = dp(14) })
                row.addView(top)
                row.addView(space(16))
                row.addView(divider())
                row.addView(space(14))
                val detail = call.optString("direction").replaceFirstChar { it.uppercase() } + " · " +
                    call.optString("status").replace('_', ' ').replaceFirstChar { it.uppercase() }
                row.addView(body(detail + "  ·  " + formatDate(call.optString("ended_at"))).apply { setPadding(0, 0, 0, 0) })
                val notesText = call.optString("notes")
                if (notesText.isNotBlank()) {
                    row.addView(space(14))
                    row.addView(body(notesText).apply { setTextColor(ink); setPadding(0, 0, 0, 0); maxLines = 3; ellipsize = android.text.TextUtils.TruncateAt.END })
                }
                row.addView(space(20))
                val buttons = LinearLayout(this@MainActivity)
                buttons.addView(secondaryButton("Call").apply { setOnClickListener { dial(call.optString("phone_number")) } },
                    LinearLayout.LayoutParams(0, dp(48), 1f).apply { rightMargin = dp(10) })
                buttons.addView(primaryButton(if (notesText.isBlank()) "Add notes" else "Edit notes").apply {
                    setOnClickListener { editCallNotes(call) }
                }, LinearLayout.LayoutParams(0, dp(48), 1f))
                row.addView(buttons)
                content.addView(row)
                content.addView(space(14))
            }
            if (calls.length() > 0) content.addView(pagination(data.optBoolean("has_next")))
        }
        root.addView(space(20))
        root.addView(quietButton("Sync calls now").apply { setOnClickListener {
            TrackingScheduler.enqueueReconcile(this@MainActivity)
            TrackingScheduler.enqueueSync(this@MainActivity)
            toast("Sync requested. Refresh to see new calls.")
        } })
    }

    private fun showReminders(root: LinearLayout, version: Int) {
        root.addView(kicker("YOUR FOLLOW-UPS"))
        root.addView(title("Reminders").apply { textSize = 36f })
        root.addView(body("The right conversation at the right time."))
        root.addView(space(26))
        val content = LinearLayout(this).apply { orientation = LinearLayout.VERTICAL }
        root.addView(content)
        loadInto(content, version, { ApiClient(this).authorizedGet(apiPath + "reminders/?page=$pageNumber") }) { data ->
            val stats = data.getJSONObject("stats")
            val summary = card()
            summary.addView(sectionTitle("Your day at a glance"))
            summary.addView(body("Synced with your SHVYA CRM."))
            summary.addView(space(20))
            summary.addView(metricRow(listOf(
                "Total" to stats.optInt("total"), "Today" to stats.optInt("today"),
                "Upcoming" to stats.optInt("upcoming"), "Overdue" to stats.optInt("overdue"),
            )))
            content.addView(summary)
            content.addView(space(32))
            content.addView(sectionHeader("Follow-ups", "Refresh").apply { setOnClickListener { render() } })
            content.addView(space(14))
            val rows = data.getJSONArray("reminders")
            if (rows.length() == 0) content.addView(emptyCard("All caught up", "Your assigned CRM reminders will appear here."))
            for (i in 0 until rows.length()) {
                val item = rows.getJSONObject(i)
                val overdue = item.optBoolean("overdue")
                val row = card()
                val heading = LinearLayout(this@MainActivity).apply { gravity = Gravity.CENTER_VERTICAL }
                heading.addView(iconBadge(R.drawable.ic_bell_outline,
                    if (overdue) Color.rgb(204, 79, 65) else blue,
                    if (overdue) Color.rgb(255, 238, 235) else Color.rgb(236, 244, 255)))
                heading.addView(LinearLayout(this@MainActivity).apply {
                    orientation = LinearLayout.VERTICAL
                    addView(sectionTitle(item.optString("lead_name")).apply {
                        setPadding(0, 0, 0, 0); maxLines = 1; ellipsize = android.text.TextUtils.TruncateAt.END
                    })
                    addView(body(if (overdue) "Overdue" else "Scheduled").apply {
                        setTextColor(if (overdue) Color.rgb(204, 79, 65) else Color.rgb(35, 139, 100))
                        setPadding(0, dp(4), 0, 0)
                    })
                }, LinearLayout.LayoutParams(0, -2, 1f).apply { leftMargin = dp(14) })
                val deleteControl = iconAction(R.drawable.ic_more_outline, "Delete reminder")
                deleteControl.setOnClickListener {
                    AlertDialog.Builder(this@MainActivity).setTitle("Delete reminder?")
                        .setMessage(item.optString("lead_name")).setNegativeButton("Cancel", null)
                        .setPositiveButton("Delete") { _, _ -> reminderAction(item, "delete", deleteControl) }.show()
                }
                heading.addView(deleteControl)
                row.addView(heading)
                row.addView(space(18))
                row.addView(sectionTitle(item.optString("title")).apply { setPadding(0, 0, 0, 0) })
                if (item.optString("description").isNotBlank()) row.addView(body(item.optString("description")))
                row.addView(space(12))
                row.addView(body(formatDate(item.optString("due_at"))).apply { setPadding(0, 0, 0, 0) })
                row.addView(space(18))
                row.addView(divider())
                row.addView(space(16))
                val actions = LinearLayout(this@MainActivity)
                actions.addView(secondaryButton("Snooze 30m").apply { setOnClickListener { reminderAction(item, "snooze", this) } },
                    LinearLayout.LayoutParams(0, dp(48), 1f).apply { rightMargin = dp(10) })
                actions.addView(primaryButton("Mark done").apply { setOnClickListener { reminderAction(item, "complete", this) } },
                    LinearLayout.LayoutParams(0, dp(48), 1f))
                row.addView(actions)
                row.addView(space(8))
                row.addView(quietButton("Call lead").apply { setOnClickListener { dial(item.optString("phone")) } })
                content.addView(row)
                content.addView(space(14))
            }
            if (rows.length() > 0) content.addView(pagination(data.optBoolean("has_next")))
        }
    }

    private fun reminderAction(item: JSONObject, action: String, button: View) {
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
        val content = dialogContent()
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
        val content = dialogContent()
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
                    minHeight = dp(64)
                    textSize = 14f
                    setTextColor(ink)
                    thumbTintList = ColorStateList.valueOf(Color.WHITE)
                    trackTintList = ColorStateList.valueOf(if (isChecked) blue else Color.rgb(196, 203, 214))
                }
                val missed = Switch(this@MainActivity).apply {
                    text = "Auto create lead · missed calls"
                    isChecked = data.optBoolean("auto_create_missed")
                    isEnabled = canEdit
                    minHeight = dp(64)
                    textSize = 14f
                    setTextColor(ink)
                    thumbTintList = ColorStateList.valueOf(Color.WHITE)
                    trackTintList = ColorStateList.valueOf(if (isChecked) blue else Color.rgb(196, 203, 214))
                }
                content.addView(sectionTitle("Lead creation"))
                content.addView(answered)
                content.addView(divider())
                if (incoming != outgoing) content.addView(body("Incoming and outgoing rules differ. Changing this switch updates both."))
                content.addView(missed)
                content.addView(divider())
                content.addView(space(14))
                content.addView(body(if (canEdit) "These settings apply to your organization." else "Your administrator manages lead creation settings."))
                var answeredChanged = false
                var missedChanged = false
                answered.setOnCheckedChangeListener { _, checked ->
                    answeredChanged = true
                    answered.trackTintList = ColorStateList.valueOf(if (checked) blue else Color.rgb(196, 203, 214))
                }
                missed.setOnCheckedChangeListener { _, checked ->
                    missedChanged = true
                    missed.trackTintList = ColorStateList.valueOf(if (checked) blue else Color.rgb(196, 203, 214))
                }
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
            content.addView(space(18))
            content.addView(sectionTitle("Phone & account"))
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
        val content = dialogContent()
        val name = field("Lead name")
        val phone = field("Phone with country code").apply { inputType = InputType.TYPE_CLASS_PHONE }
        val message = body("")
        content.addView(body("Create a lead in your SHVYA CRM."))
        content.addView(space(20))
        content.addView(fieldLabel("Name")); content.addView(space(8)); content.addView(name)
        content.addView(space(18))
        content.addView(fieldLabel("Phone number")); content.addView(space(8)); content.addView(phone)
        content.addView(space(8)); content.addView(message)
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
        gravity = Gravity.CENTER_VERTICAL
        values.forEachIndexed { index, (label, value) ->
            val column = LinearLayout(this@MainActivity).apply {
                orientation = LinearLayout.VERTICAL
                gravity = Gravity.CENTER_HORIZONTAL
                addView(TextView(this@MainActivity).apply {
                    text = value.toString()
                    textSize = 25f
                    setTypeface(typeface, Typeface.BOLD)
                    setTextColor(when (label) {
                        "Missed", "Overdue" -> Color.rgb(204, 79, 65)
                        "Upcoming" -> Color.rgb(35, 139, 100)
                        else -> ink
                    })
                    includeFontPadding = false
                })
                addView(body(label).apply {
                    textSize = 11f
                    gravity = Gravity.CENTER
                    setPadding(0, dp(8), 0, 0)
                })
            }
            addView(column, LinearLayout.LayoutParams(0, -2, 1f))
            if (index < values.lastIndex) addView(View(this@MainActivity).apply {
                setBackgroundColor(line)
            }, LinearLayout.LayoutParams(dp(1), dp(44)))
        }
    }

    private fun outcome(label: String, count: Int, tint: Int): TextView = TextView(this).apply {
        text = "$label  $count"
        textSize = 13f
        setTextColor(tint)
        setTypeface(typeface, Typeface.BOLD)
        gravity = Gravity.CENTER
    }

    private fun divider(): View = View(this).apply {
        setBackgroundColor(line)
        layoutParams = LinearLayout.LayoutParams(-1, dp(1))
    }

    private fun sectionHeader(heading: String, action: String): LinearLayout = LinearLayout(this).apply {
        gravity = Gravity.CENTER_VERTICAL
        addView(sectionTitle(heading).apply { textSize = 21f; setPadding(0, 0, 0, 0) },
            LinearLayout.LayoutParams(0, -2, 1f))
        addView(TextView(this@MainActivity).apply {
            text = action
            textSize = 14f
            setTypeface(typeface, Typeface.BOLD)
            setTextColor(blue)
            gravity = Gravity.CENTER
            minimumHeight = dp(44)
        })
    }

    private fun iconBadge(drawable: Int, tint: Int, backgroundColor: Int): ImageView = ImageView(this).apply {
        setImageResource(drawable)
        imageTintList = ColorStateList.valueOf(tint)
        setPadding(dp(12), dp(12), dp(12), dp(12))
        background = rounded(backgroundColor, 15f)
        layoutParams = LinearLayout.LayoutParams(dp(46), dp(46))
    }

    private fun iconAction(drawable: Int, description: String): ImageView = ImageView(this).apply {
        setImageResource(drawable)
        imageTintList = ColorStateList.valueOf(blue)
        setPadding(dp(14), dp(14), dp(14), dp(14))
        contentDescription = description
        background = RippleDrawable(ColorStateList.valueOf(Color.rgb(210, 223, 241)),
            rounded(Color.rgb(235, 242, 251), 17f), null)
        layoutParams = LinearLayout.LayoutParams(dp(48), dp(48))
    }

    private fun pagination(hasNext: Boolean): LinearLayout = LinearLayout(this).apply {
        gravity = Gravity.CENTER
        if (pageNumber > 1) addView(quietButton("Previous").apply { setOnClickListener { pageNumber--; render() } })
        addView(body("Page $pageNumber").apply { setPadding(dp(14), 0, dp(14), 0) })
        if (hasNext) addView(quietButton("Next").apply { setOnClickListener { pageNumber++; render() } })
    }

    private fun emptyCard(heading: String, text: String): LinearLayout = card().apply {
        gravity = Gravity.CENTER
        setPadding(dp(30), dp(38), dp(30), dp(38))
        addView(iconBadge(R.drawable.ic_phone_outline, blue, Color.rgb(236, 244, 255)))
        addView(space(20))
        addView(sectionTitle(heading).apply { textSize = 21f; gravity = Gravity.CENTER })
        addView(body(text).apply { gravity = Gravity.CENTER })
    }

    private fun setDashboardPage(content: LinearLayout) {
        val shell = LinearLayout(this).apply {
            orientation = LinearLayout.VERTICAL
            setBackgroundColor(canvas)
        }
        shell.addView(ScrollView(this).apply {
            fillViewport = true
            clipToPadding = false
            addView(content)
        }, LinearLayout.LayoutParams(-1, 0, 1f))
        shell.addView(divider())
        val nav = LinearLayout(this).apply {
            setPadding(dp(22), dp(7), dp(22), dp(7))
            setBackgroundColor(Color.WHITE)
        }
        listOf(Triple("home", "Calls", R.drawable.ic_phone_outline),
            Triple("reminders", "Reminders", R.drawable.ic_bell_outline)).forEach { (key, label, drawable) ->
            val active = selectedTab == key
            val item = LinearLayout(this).apply {
                orientation = LinearLayout.VERTICAL
                gravity = Gravity.CENTER
                minimumHeight = dp(58)
                contentDescription = label
                addView(ImageView(this@MainActivity).apply {
                    setImageResource(drawable)
                    imageTintList = ColorStateList.valueOf(if (active) blue else muted)
                }, LinearLayout.LayoutParams(dp(23), dp(23)))
                addView(TextView(this@MainActivity).apply {
                    text = label
                    textSize = 11f
                    gravity = Gravity.CENTER
                    setPadding(0, dp(5), 0, 0)
                    setTypeface(typeface, if (active) Typeface.BOLD else Typeface.NORMAL)
                    setTextColor(if (active) blue else muted)
                })
                setOnClickListener { selectedTab = key; pageNumber = 1; render() }
            }
            nav.addView(item, LinearLayout.LayoutParams(0, -2, 1f))
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
            setPadding(dp(22), dp(18), dp(22), dp(36))
            setBackgroundColor(canvas)
        }

    private fun setPage(content: LinearLayout) {
        val scroll = ScrollView(this)
        scroll.addView(content)
        setContentView(scroll)
    }

    private fun card(): LinearLayout =
        LinearLayout(this).apply {
            orientation = LinearLayout.VERTICAL
            setPadding(dp(22), dp(22), dp(22), dp(22))
            background = rounded(Color.WHITE, 23f)
            elevation = dp(1).toFloat()
        }

    private fun dialogContent(): LinearLayout = LinearLayout(this).apply {
        orientation = LinearLayout.VERTICAL
        setPadding(dp(24), dp(12), dp(24), dp(20))
    }

    private fun kicker(text: String): TextView =
        TextView(this).apply {
            this.text = text
            textSize = 11f
            setTextColor(blue)
            setTypeface(typeface, Typeface.BOLD)
            letterSpacing = .1f
        }

    private fun title(text: String): TextView =
        TextView(this).apply {
            this.text = text
            textSize = 38f
            setTextColor(ink)
            setTypeface(typeface, Typeface.BOLD)
            setPadding(0, dp(7), 0, 0)
        }

    private fun sectionTitle(text: String): TextView =
        TextView(this).apply {
            this.text = text
            textSize = 17f
            setTextColor(ink)
            setTypeface(typeface, Typeface.BOLD)
            setPadding(0, 0, 0, dp(3))
        }

    private fun body(text: String): TextView =
        TextView(this).apply {
            this.text = text
            textSize = 14f
            setTextColor(muted)
            setLineSpacing(dp(2).toFloat(), 1f)
            setPadding(0, dp(4), 0, 0)
        }

    private fun fieldLabel(label: String): TextView = TextView(this).apply {
        text = label
        textSize = 13f
        setTypeface(typeface, Typeface.BOLD)
        setTextColor(ink)
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
            setTextColor(ink)
            setHintTextColor(Color.rgb(145, 145, 150))
            setPadding(dp(16), 0, dp(16), 0)
            minHeight = dp(54)
            background = rounded(Color.rgb(241, 243, 247), 15f)
        }

    private fun primaryButton(label: String): Button =
        Button(this).apply {
            text = label
            isAllCaps = false
            textSize = 14f
            setTypeface(typeface, Typeface.BOLD)
            setTextColor(Color.WHITE)
            minHeight = dp(48)
            minWidth = 0
            minimumWidth = 0
            setPadding(dp(12), 0, dp(12), 0)
            backgroundTintList = null
            background = RippleDrawable(ColorStateList.valueOf(Color.rgb(149, 188, 235)), rounded(blue, 15f), null)
        }

    private fun secondaryButton(label: String): Button =
        Button(this).apply {
            text = label
            isAllCaps = false
            textSize = 14f
            setTypeface(typeface, Typeface.BOLD)
            setTextColor(blue)
            minHeight = dp(48)
            minWidth = 0
            minimumWidth = 0
            setPadding(dp(12), 0, dp(12), 0)
            backgroundTintList = null
            background = RippleDrawable(ColorStateList.valueOf(Color.rgb(205, 222, 245)),
                rounded(Color.rgb(234, 242, 253), 15f), null)
        }

    private fun quietButton(label: String): Button = Button(this).apply {
        text = label
        isAllCaps = false
        textSize = 13f
        setTypeface(typeface, Typeface.BOLD)
        setTextColor(blue)
        minHeight = dp(44)
        minWidth = 0
        minimumWidth = 0
        setPadding(dp(8), 0, dp(8), 0)
        backgroundTintList = null
        background = RippleDrawable(ColorStateList.valueOf(Color.rgb(223, 234, 248)),
            rounded(Color.TRANSPARENT, 12f), null)
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
