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
import android.text.method.HideReturnsTransformationMethod
import android.text.method.PasswordTransformationMethod
import android.view.Gravity
import android.view.View
import android.widget.Button
import android.widget.ImageView
import android.widget.Switch
import android.app.DatePickerDialog
import android.app.AlertDialog
import android.app.TimePickerDialog
import android.widget.Toast
import androidx.core.view.ViewCompat
import androidx.core.view.WindowInsetsCompat
import androidx.work.WorkManager
import org.json.JSONObject
import java.util.Calendar
import java.util.Date
import java.util.Locale
import java.util.TimeZone
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
import com.shvya.callintelligence.data.AppDatabase
import com.shvya.callintelligence.data.PendingAction
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
    private var selectedTab = "today"
    private var query = ""
    private var callFilter = "all"
    private var reminderSegment = "today"
    private var leadQuery = ""
    private var pendingSyncCount = 0
    private var leadPrefillName = ""
    private var leadPrefillPhone = ""
    private var pendingOpenCallId = ""
    private var pendingCallAction = ""
    private var pendingOpenLeadId = ""
    private var pendingReminderId = ""
    private var pendingReminderAction = ""
    private var dateFrom = ""
    private var dateTo = ""
    private var pageNumber = 1
    private var renderVersion = 0
    private var leadFormOpen = false
    private var leadDetailId = ""
    private val apiPath = "api/v1/call-intelligence/"

    override fun onResume() {
        super.onResume()
        if (::auth.isInitialized && auth.hasSession()) {
            refreshLocalSyncHealth()
            render()
        }
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
        leadFormOpen = savedInstanceState?.getBoolean("lead_form_open") ?: false
        handleLaunchIntent(intent)
        render()
        openPendingCallAfterRender()
    }

    override fun onNewIntent(intent: Intent) {
        super.onNewIntent(intent)
        setIntent(intent)
        handleLaunchIntent(intent)
        render()
        openPendingCallAfterRender()
    }

    private fun handleLaunchIntent(intent: Intent?) {
        pendingOpenCallId = intent?.getStringExtra("open_call_id").orEmpty()
        pendingCallAction = intent?.getStringExtra("call_action").orEmpty()
        pendingOpenLeadId = intent?.getStringExtra("open_lead_id").orEmpty()
        pendingReminderId = intent?.getStringExtra("reminder_id").orEmpty()
        pendingReminderAction = intent?.getStringExtra("reminder_action").orEmpty()
        if (intent?.getBooleanExtra("post_call_review", false) == true) {
            selectedTab = "calls"
        }
        if (intent?.getBooleanExtra("open_followups", false) == true) {
            selectedTab = "followups"
            reminderSegment = "today"
        }
    }

    private fun openPendingCallAfterRender() {
        val callId = pendingOpenCallId
        val callAction = pendingCallAction
        if (callId.isNotBlank() && auth.hasSession()) {
            pendingOpenCallId = ""
            pendingCallAction = ""
            window.decorView.post {
                when (callAction) {
                    "note" -> openCallNoteAction(callId)
                    "remind_tomorrow" -> scheduleCallFollowUp(callId, tomorrowAtTen())
                    else -> showCallDetail(callId)
                }
            }
        }
        val leadId = pendingOpenLeadId
        if (leadId.isNotBlank() && auth.hasSession()) {
            pendingOpenLeadId = ""
            window.decorView.post { showLeadDetail(leadId) }
        }
        val reminderId = pendingReminderId
        val reminderAction = pendingReminderAction
        if (reminderId.isNotBlank() && reminderAction.isNotBlank() && auth.hasSession()) {
            pendingReminderId = ""
            pendingReminderAction = ""
            window.decorView.post { performReminderAction(reminderId, reminderAction) }
        }
    }

    override fun onSaveInstanceState(outState: Bundle) {
        outState.putBoolean("lead_form_open", leadFormOpen)
        super.onSaveInstanceState(outState)
    }

    @Suppress("DEPRECATION")
    override fun onBackPressed() {
        when {
            leadFormOpen -> {
                leadFormOpen = false
                render()
            }
            leadDetailId.isNotBlank() -> {
                leadDetailId = ""
                render()
            }
            else -> super.onBackPressed()
        }
    }

    private fun render() {
        when {
            !auth.hasSession() -> showLogin()
            leadFormOpen -> showLeadForm()
            leadDetailId.isNotBlank() -> showLeadDetailPage(leadDetailId)
            else -> showDashboard()
        }
    }

    private fun showLogin() {
        val root = page().apply {
            gravity = Gravity.CENTER_VERTICAL
            setPadding(dp(24), dp(24), dp(24), dp(28))
        }
        root.addView(ImageView(this).apply {
            setImageResource(R.drawable.ic_shvya)
            contentDescription = "SHVYA"
            layoutParams = LinearLayout.LayoutParams(dp(62), dp(62)).apply {
                gravity = Gravity.CENTER_HORIZONTAL
            }
        })
        root.addView(space(16))
        root.addView(title("Sign in to SHVYA").apply {
            gravity = Gravity.CENTER
            textSize = 30f
            setPadding(0, 0, 0, 0)
        })
        root.addView(body("Use your SHVYA work account.").apply {
            gravity = Gravity.CENTER
            textSize = 14f
            setPadding(0, dp(7), 0, 0)
        })
        root.addView(space(24))

        val card = card().apply {
            setPadding(dp(22), dp(22), dp(22), dp(22))
            val screenWidth = resources.displayMetrics.widthPixels
            layoutParams = LinearLayout.LayoutParams(
                minOf(screenWidth - dp(48), dp(420)),
                LinearLayout.LayoutParams.WRAP_CONTENT,
            ).apply { gravity = Gravity.CENTER_HORIZONTAL }
        }
        val (emailShell, email) = loginTextField(
            hintText = "name@company.com",
            inputTypeValue = InputType.TYPE_CLASS_TEXT or InputType.TYPE_TEXT_VARIATION_EMAIL_ADDRESS,
            autofillHint = if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.O) View.AUTOFILL_HINT_EMAIL_ADDRESS else null,
        )
        email.setText(auth.email)
        val (passwordShell, password) = passwordField()
        val message = body("").apply {
            setPadding(0, dp(12), 0, 0)
            gravity = Gravity.CENTER
        }
        val login = primaryButton("Continue").apply { minimumHeight = dp(54) }

        login.setOnClickListener {
            val emailValue = email.text.toString().trim()
            val passwordValue = password.text.toString()
            if (emailValue.isBlank() || passwordValue.isBlank()) {
                message.text = "Enter your email and password."
                message.setTextColor(Color.rgb(196, 58, 54))
                return@setOnClickListener
            }
            login.isEnabled = false
            login.alpha = .72f
            login.text = "Signing in…"
            message.text = ""
            lifecycleScope.launch {
                val result = withContext(Dispatchers.IO) {
                    runCatching { ApiClient(this@MainActivity).login(emailValue, passwordValue) }
                        .getOrElse {
                            com.shvya.callintelligence.net.LoginResult(
                                false,
                                "Could not reach SHVYA. Check your internet connection and try again."
                            )
                        }
                }
                if (result.successful) {
                    render()
                } else {
                    login.isEnabled = true
                    login.alpha = 1f
                    login.text = "Continue"
                    message.text = result.message
                    message.setTextColor(Color.rgb(196, 58, 54))
                }
            }
        }

        card.addView(fieldLabel("Work email"))
        card.addView(space(8))
        card.addView(emailShell, LinearLayout.LayoutParams(-1, dp(56)))
        card.addView(space(16))
        card.addView(fieldLabel("Password"))
        card.addView(space(8))
        card.addView(passwordShell, LinearLayout.LayoutParams(-1, dp(56)))
        card.addView(space(20))
        card.addView(login, LinearLayout.LayoutParams(-1, dp(54)))
        card.addView(message)
        root.addView(card)
        setPage(root)
    }

    private fun showDashboard() {
        val version = ++renderVersion
        val root = page()
        val header = LinearLayout(this).apply {
            gravity = Gravity.CENTER_VERTICAL
            minimumHeight = dp(52)
        }
        header.addView(ImageView(this).apply {
            setImageResource(R.drawable.ic_shvya)
            contentDescription = "SHVYA"
        }, LinearLayout.LayoutParams(dp(34), dp(34)))
        header.addView(LinearLayout(this).apply {
            orientation = LinearLayout.VERTICAL
            setPadding(dp(10), 0, 0, 0)
            addView(sectionTitle("Call Intelligence").apply {
                setPadding(0, 0, 0, 0)
                textSize = 17f
            })
            addView(body("SHVYA").apply {
                textSize = 11f
                setPadding(0, dp(1), 0, 0)
            })
        }, LinearLayout.LayoutParams(0, -2, 1f))
        header.addView(iconAction(R.drawable.ic_settings_outline, "Settings").apply {
            setPadding(dp(12), dp(12), dp(12), dp(12))
            layoutParams = LinearLayout.LayoutParams(dp(44), dp(44))
            setOnClickListener { showSettings() }
        })
        root.addView(header)
        root.addView(space(20))

        if (!essentialPermissionsGranted()) {
            val permissionCard = card()
            permissionCard.addView(iconBadge(R.drawable.ic_phone_outline, blue, Color.rgb(238, 244, 253)))
            permissionCard.addView(space(16))
            permissionCard.addView(sectionTitle("Finish setup").apply { textSize = 22f })
            permissionCard.addView(body("Allow phone and call-log access so SHVYA can organize your call activity automatically."))
            permissionCard.addView(space(18))
            permissionCard.addView(primaryButton("Allow call access").apply {
                setOnClickListener { permissionLauncher.launch(requiredPermissions()) }
            })
            permissionCard.addView(space(8))
            permissionCard.addView(quietButton("Open Android settings").apply {
                setOnClickListener {
                    startActivity(Intent(Settings.ACTION_APPLICATION_DETAILS_SETTINGS, Uri.parse("package:$packageName")))
                }
            })
            root.addView(permissionCard)
        } else {
            when (selectedTab) {
                "today" -> showToday(root, version)
                "followups" -> showReminders(root, version)
                "leads" -> showLeads(root, version)
                else -> showCalls(root, version)
            }
        }
        setDashboardPage(root)
        if (essentialPermissionsGranted()) {
            TrackingScheduler.ensureBaseline(this)
            TrackingScheduler.scheduleRecurring(this)
        }
    }

    private fun showToday(root: LinearLayout, version: Int) {
        root.addView(kicker("TODAY"))
        root.addView(title("Today").apply { textSize = 34f })
        root.addView(body("Your calls and follow-ups, focused on what needs attention."))
        root.addView(space(20))
        if (pendingSyncCount > 0) {
            root.addView(syncHealthCard())
            root.addView(space(16))
        }

        val content = LinearLayout(this).apply { orientation = LinearLayout.VERTICAL }
        root.addView(content)
        loadInto(content, version, { ApiClient(this).authorizedGet(apiPath + "today/?mine=1") }) { data ->
            val stats = data.getJSONObject("stats")
            val summary = card()
            summary.addView(sectionTitle(
                if (stats.optInt("overdue") > 0 || stats.optInt("missed_needing_action") > 0) {
                    "Needs your attention"
                } else {
                    "You're on track"
                }
            ))
            summary.addView(body(
                if (stats.optInt("overdue") > 0 || stats.optInt("missed_needing_action") > 0) {
                    "Clear the important items first, then continue with today's calls."
                } else {
                    "No urgent call or follow-up issues are waiting right now."
                }
            ))
            summary.addView(space(18))
            summary.addView(metricRow(listOf(
                "Calls" to stats.optInt("total"),
                "Missed" to stats.optInt("missed_needing_action"),
                "Due" to stats.optInt("followups_due"),
                "Overdue" to stats.optInt("overdue"),
            )))
            content.addView(summary)
            content.addView(space(16))

            val actions = LinearLayout(this@MainActivity)
            actions.addView(secondaryButton("Missed calls").apply {
                setOnClickListener {
                    selectedTab = "calls"
                    callFilter = "missed"
                    pageNumber = 1
                    render()
                }
            }, LinearLayout.LayoutParams(0, dp(48), 1f).apply { rightMargin = dp(8) })
            actions.addView(primaryButton("Follow-ups").apply {
                setOnClickListener {
                    selectedTab = "followups"
                    reminderSegment = "today"
                    pageNumber = 1
                    render()
                }
            }, LinearLayout.LayoutParams(0, dp(48), 1f))
            content.addView(actions)
            content.addView(space(26))

            content.addView(sectionHeader("Recent calls", "See all").apply {
                setOnClickListener { selectedTab = "calls"; callFilter = "all"; render() }
            })
            content.addView(space(12))
            val calls = data.optJSONArray("recent_calls") ?: org.json.JSONArray()
            if (calls.length() == 0) {
                content.addView(emptyCard("No calls today", "Your calls will appear here as they are captured."))
            } else {
                for (i in 0 until calls.length()) {
                    content.addView(compactCallCard(calls.getJSONObject(i)))
                    content.addView(space(10))
                }
            }

            content.addView(space(18))
            content.addView(sectionHeader("Next follow-ups", "Open").apply {
                setOnClickListener { selectedTab = "followups"; render() }
            })
            content.addView(space(12))
            val reminders = data.optJSONArray("reminders") ?: org.json.JSONArray()
            if (reminders.length() == 0) {
                content.addView(emptyCard("Nothing due", "You are clear for now. New follow-ups will appear here."))
            } else {
                for (i in 0 until reminders.length()) {
                    content.addView(compactReminderCard(reminders.getJSONObject(i)))
                    content.addView(space(10))
                }
            }
            loadTeamPerformance(content, version)
        }
    }

    private fun showLeads(root: LinearLayout, version: Int) {
        root.addView(kicker("CRM"))
        root.addView(title("Leads").apply { textSize = 34f })
        root.addView(body("Call, message, follow up and move leads without leaving your phone."))
        root.addView(space(22))

        val search = field("Search name, phone or email").apply {
            setText(leadQuery)
            isSingleLine = true
            imeOptions = android.view.inputmethod.EditorInfo.IME_ACTION_SEARCH
            setCompoundDrawablesWithIntrinsicBounds(R.drawable.ic_search_outline, 0, 0, 0)
            compoundDrawablePadding = dp(10)
            setOnEditorActionListener { _, _, _ ->
                leadQuery = text.toString().trim()
                rememberLeadSearch(leadQuery)
                pageNumber = 1
                render()
                true
            }
        }
        val row = LinearLayout(this).apply { gravity = Gravity.CENTER_VERTICAL }
        row.addView(search, LinearLayout.LayoutParams(0, dp(54), 1f))
        row.addView(iconAction(R.drawable.ic_plus_outline, "Add lead").apply {
            setOnClickListener { addLead() }
        }, LinearLayout.LayoutParams(dp(54), dp(54)).apply { leftMargin = dp(10) })
        root.addView(row)

        val recent = recentLeadSearches()
        if (leadQuery.isBlank() && recent.isNotEmpty()) {
            root.addView(space(10))
            val recentRow = LinearLayout(this).apply { gravity = Gravity.CENTER_VERTICAL }
            recent.take(3).forEach { value ->
                recentRow.addView(quietButton(value).apply {
                    setOnClickListener { leadQuery = value; render() }
                }, LinearLayout.LayoutParams(0, dp(42), 1f))
            }
            root.addView(recentRow)
        }
        if (leadQuery.isNotBlank()) {
            root.addView(quietButton("Clear search").apply {
                setOnClickListener { leadQuery = ""; render() }
            })
        }
        root.addView(space(18))

        val content = LinearLayout(this).apply { orientation = LinearLayout.VERTICAL }
        root.addView(content)
        val path = apiPath + "leads/list/?q=" + URLEncoder.encode(leadQuery, "UTF-8")
        loadInto(content, version, { ApiClient(this).authorizedGet(path) }) { data ->
            val rows = data.optJSONArray("results") ?: org.json.JSONArray()
            if (rows.length() == 0) {
                content.addView(emptyCard("No leads found", "Try another search or create a new CRM lead."))
                return@loadInto
            }
            val leads = mutableListOf<JSONObject>()
            for (i in 0 until rows.length()) leads.add(rows.getJSONObject(i))
            val favorites = favoriteLeadIds()
            leads.sortWith(compareByDescending<JSONObject> { favorites.contains(it.optString("id")) }
                .thenBy { it.optString("name").lowercase(Locale.getDefault()) })
            leads.forEach { lead ->
                val leadRow = leadCard(lead, favorites.contains(lead.optString("id")))
                leadRow.setOnClickListener { showLeadDetail(lead.optString("id")) }
                content.addView(leadRow)
                content.addView(space(10))
            }
        }
    }

    private fun compactCallCard(call: JSONObject): LinearLayout = card().apply {
        val lead = call.optJSONObject("lead")
        val name = call.optString("contact_name").ifBlank { lead?.optString("name").orEmpty() }
            .ifBlank { call.optString("phone_number") }
        val top = LinearLayout(this@MainActivity).apply { gravity = Gravity.CENTER_VERTICAL }
        val missed = call.optString("status") == "missed"
        top.addView(iconBadge(
            R.drawable.ic_phone_outline,
            if (missed) Color.rgb(204, 79, 65) else blue,
            if (missed) Color.rgb(255, 238, 235) else Color.rgb(236, 244, 255),
        ))
        top.addView(LinearLayout(this@MainActivity).apply {
            orientation = LinearLayout.VERTICAL
            addView(sectionTitle(name).apply {
                setPadding(0, 0, 0, 0)
                maxLines = 1
                ellipsize = android.text.TextUtils.TruncateAt.END
            })
            addView(body(
                call.optString("direction").replaceFirstChar { it.uppercase() } + " · " +
                    call.optString("status").replace('_', ' ').replaceFirstChar { it.uppercase() } +
                    " · " + call.optString("talk_duration").ifBlank { formatDate(call.optString("ended_at")) }
            ).apply { setPadding(0, dp(3), 0, 0) })
        }, LinearLayout.LayoutParams(0, -2, 1f).apply { leftMargin = dp(13) })
        addView(top)
        if (lead != null) {
            addView(space(12))
            addView(body(lead.optString("pipeline") + "  ›  " + lead.optString("stage")).apply {
                setTextColor(blue)
                setPadding(0, 0, 0, 0)
            })
        }
        setOnClickListener { showCallDetail(call.optString("id")) }
    }

    private fun compactReminderCard(item: JSONObject): LinearLayout = card().apply {
        val overdue = item.optBoolean("overdue")
        val top = LinearLayout(this@MainActivity).apply { gravity = Gravity.CENTER_VERTICAL }
        top.addView(iconBadge(
            R.drawable.ic_bell_outline,
            if (overdue) Color.rgb(204, 79, 65) else blue,
            if (overdue) Color.rgb(255, 238, 235) else Color.rgb(236, 244, 255),
        ))
        top.addView(LinearLayout(this@MainActivity).apply {
            orientation = LinearLayout.VERTICAL
            addView(sectionTitle(item.optString("lead_name")).apply { setPadding(0, 0, 0, 0) })
            addView(body(
                (if (overdue) "Overdue · " else "") + formatDate(item.optString("due_at"))
            ).apply {
                setTextColor(if (overdue) Color.rgb(204, 79, 65) else Color.rgb(35, 139, 100))
                setPadding(0, dp(3), 0, 0)
            })
        }, LinearLayout.LayoutParams(0, -2, 1f).apply { leftMargin = dp(13) })
        addView(top)
        addView(space(12))
        addView(body(item.optString("title")).apply { setTextColor(ink); setPadding(0, 0, 0, 0) })
        val leadId = item.optString("lead_id")
        if (leadId.isNotBlank()) setOnClickListener { showLeadDetail(leadId) }
    }

    private fun leadCard(lead: JSONObject, favorite: Boolean): LinearLayout = card().apply {
        val top = LinearLayout(this@MainActivity).apply { gravity = Gravity.CENTER_VERTICAL }
        top.addView(iconBadge(R.drawable.ic_people_outline, blue, Color.rgb(236, 244, 255)))
        top.addView(LinearLayout(this@MainActivity).apply {
            orientation = LinearLayout.VERTICAL
            addView(sectionTitle((if (favorite) "★  " else "") + lead.optString("name")).apply {
                setPadding(0, 0, 0, 0)
                maxLines = 1
                ellipsize = android.text.TextUtils.TruncateAt.END
            })
            addView(body(lead.optString("phone")).apply { setPadding(0, dp(3), 0, 0) })
        }, LinearLayout.LayoutParams(0, -2, 1f).apply { leftMargin = dp(13) })
        addView(top)
        addView(space(12))
        addView(body(lead.optString("pipeline") + "  ›  " + lead.optString("stage")).apply {
            setTextColor(blue)
            setPadding(0, 0, 0, 0)
        })
        val reminder = lead.optJSONObject("reminder")
        if (reminder != null) {
            addView(space(8))
            addView(body(
                (if (reminder.optBoolean("overdue")) "Overdue · " else "Follow-up · ") +
                    formatDate(reminder.optString("due_at"))
            ).apply {
                setTextColor(if (reminder.optBoolean("overdue")) Color.rgb(204, 79, 65) else Color.rgb(35, 139, 100))
                setPadding(0, 0, 0, 0)
            })
        }
    }

    private fun syncHealthCard(): LinearLayout = card().apply {
        val pending = pendingSyncCount
        addView(sectionTitle(
            if (pending == 0) "Sync healthy"
            else pending.toString() + " item" + (if (pending == 1) "" else "s") + " waiting to sync"
        ))
        addView(body(
            if (pending == 0) "Calls are synced with SHVYA CRM."
            else "Nothing is lost. Pending call activity will sync automatically when connectivity is available."
        ))
        addView(space(12))
        addView(secondaryButton("Sync now").apply {
            setOnClickListener {
                TrackingScheduler.enqueueReconcile(this@MainActivity)
                TrackingScheduler.enqueueSync(this@MainActivity)
                toast("Sync requested")
                refreshLocalSyncHealth()
            }
        })
    }

    private fun refreshLocalSyncHealth() {
        if (!::auth.isInitialized || !auth.hasSession()) return
        lifecycleScope.launch {
            val count = withContext(Dispatchers.IO) {
                runCatching { AppDatabase.get(this@MainActivity).callDao().pendingCount() }.getOrDefault(0)
            }
            pendingSyncCount = count
        }
    }

    private fun loadTeamPerformance(content: LinearLayout, version: Int) {
        lifecycleScope.launch {
            val today = SimpleDateFormat("yyyy-MM-dd", Locale.US).format(Date())
            val personalResponse = withContext(Dispatchers.IO) {
                runCatching {
                    ApiClient(this@MainActivity).authorizedGet(
                        apiPath + "analytics/?mine=1&date_from=" + today + "&date_to=" + today
                    )
                }.getOrNull()
            }
            if (version != renderVersion || personalResponse?.successful != true) return@launch
            val personal = runCatching { JSONObject(personalResponse.body) }.getOrNull() ?: return@launch
            val stats = personal.optJSONObject("stats")
            if (stats != null) {
                content.addView(space(24))
                val performance = card()
                performance.addView(sectionTitle("Performance today").apply { textSize = 20f })
                performance.addView(body("Your daily calling rhythm and sales follow-through."))
                performance.addView(space(16))
                performance.addView(metricRow(listOf(
                    "Calls" to stats.optInt("total"),
                    "Answered" to stats.optInt("answered"),
                    "Follow-ups" to stats.optInt("followups"),
                    "Overdue" to stats.optInt("overdue"),
                )))
                performance.addView(space(14))
                performance.addView(body(
                    "Answer rate " + stats.optDouble("answer_rate").toInt() + "% · Talk time " +
                        formatDurationSeconds(stats.optInt("total_talk")) + " · Conversion " +
                        stats.optDouble("conversion_rate").toInt() + "%"
                ).apply { setPadding(0, 0, 0, 0) })
                content.addView(performance)
            }

            val teamResponse = withContext(Dispatchers.IO) {
                runCatching {
                    ApiClient(this@MainActivity).authorizedGet(
                        apiPath + "analytics/?date_from=" + today + "&date_to=" + today
                    )
                }.getOrNull()
            }
            if (version != renderVersion || teamResponse?.successful != true) return@launch
            val teamData = runCatching { JSONObject(teamResponse.body) }.getOrNull() ?: return@launch
            val agents = teamData.optJSONArray("agents") ?: return@launch
            if (agents.length() <= 1) return@launch
            content.addView(space(24))
            content.addView(sectionTitle("Team today").apply { textSize = 20f })
            content.addView(body("Calls and follow-up activity across your team."))
            content.addView(space(10))
            for (i in 0 until minOf(agents.length(), 8)) {
                val agent = agents.getJSONObject(i)
                val row = card()
                row.addView(sectionTitle(agent.optString("user__name").ifBlank { agent.optString("user__email") }).apply {
                    setPadding(0, 0, 0, 0)
                })
                row.addView(body(
                    agent.optInt("calls").toString() + " calls · " +
                        agent.optDouble("answer_rate").toInt() + "% answered · " +
                        agent.optInt("followups") + " due"
                ))
                content.addView(row)
                content.addView(space(8))
            }
        }
    }

    private fun showCalls(root: LinearLayout, version: Int) {
        root.addView(kicker("YOUR WORKSPACE"))
        root.addView(title("Calls").apply { textSize = 34f })
        root.addView(body("Find a call, take the next action and keep CRM updated."))
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
        val filterRow = LinearLayout(this).apply { gravity = Gravity.CENTER_VERTICAL }
        filterRow.addView(secondaryButton(callFilterLabel() + "  ⌄").apply {
            setOnClickListener { showCallFilterPicker() }
        }, LinearLayout.LayoutParams(0, dp(46), 1f).apply { rightMargin = dp(8) })
        filterRow.addView(secondaryButton(callDateLabel() + "  ⌄").apply {
            setOnClickListener { showCallDatePicker() }
        }, LinearLayout.LayoutParams(0, dp(46), 1f))
        root.addView(filterRow)
        if (query.isNotBlank() || callFilter != "all" || dateFrom.isNotBlank()) {
            root.addView(space(4))
            root.addView(quietButton("Reset search & filters").apply {
                setOnClickListener {
                    query = ""
                    callFilter = "all"
                    dateFrom = ""
                    dateTo = ""
                    pageNumber = 1
                    render()
                }
            })
        }
        root.addView(space(20))

        val content = LinearLayout(this).apply { orientation = LinearLayout.VERTICAL }
        root.addView(content)
        var path = apiPath + "calls/?mine=1&page=" + pageNumber + "&q=" + URLEncoder.encode(query, "UTF-8") +
            "&date_from=" + dateFrom + "&date_to=" + dateTo
        path += when (callFilter) {
            "answered" -> "&status=answered"
            "missed" -> "&status=missed"
            "followup" -> "&needs_follow_up=1"
            "incoming" -> "&direction=incoming"
            "outgoing" -> "&direction=outgoing"
            "linked" -> "&linked=1"
            "unknown" -> "&unlinked=1"
            else -> ""
        }
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
                row.setOnClickListener { showCallDetail(call.optString("id")) }
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
        root.addView(title("Follow-ups").apply { textSize = 34f })
        root.addView(body("Overdue, due today and upcoming work — clearly separated."))
        root.addView(space(16))
        val segments = LinearLayout(this).apply { gravity = Gravity.CENTER_VERTICAL }
        listOf(
            "overdue" to "Overdue",
            "today" to "Today",
            "upcoming" to "Upcoming",
            "completed" to "Done",
        ).forEach { (key, label) ->
            segments.addView(quietButton(if (reminderSegment == key) "•  " + label else label).apply {
                setOnClickListener { reminderSegment = key; pageNumber = 1; render() }
            }, LinearLayout.LayoutParams(0, dp(42), 1f))
        }
        root.addView(segments)
        root.addView(space(18))
        val content = LinearLayout(this).apply { orientation = LinearLayout.VERTICAL }
        root.addView(content)
        val reminderPath = if (reminderSegment == "completed") {
            apiPath + "reminders/?status=completed&page=" + pageNumber
        } else {
            apiPath + "reminders/?segment=" + reminderSegment + "&page=" + pageNumber
        }
        loadInto(content, version, { ApiClient(this).authorizedGet(reminderPath) }) { data ->
            val stats = data.getJSONObject("stats")
            val summary = card()
            summary.addView(sectionTitle("Call reminders"))
            summary.addView(body("Synced from your SHVYA CRM pipelines."))
            summary.addView(space(20))
            summary.addView(metricRow(listOf(
                "Total" to stats.optInt("total"), "Upcoming" to stats.optInt("upcoming"),
                "Overdue" to stats.optInt("overdue"), "Today" to stats.optInt("today"),
            )))
            content.addView(summary)
            content.addView(space(32))
            content.addView(sectionHeader(
                if (reminderSegment == "completed") "Completed follow-ups" else "Reminder list",
                "Refresh"
            ).apply { setOnClickListener { render() } })
            content.addView(space(14))
            val rows = data.getJSONArray("reminders")
            if (rows.length() == 0) content.addView(emptyCard("All caught up", "Reminders for leads in your CRM pipelines will appear here."))
            for (i in 0 until rows.length()) {
                val item = rows.getJSONObject(i)
                val overdue = item.optBoolean("overdue")
                val row = card()
                if (overdue) row.background = rounded(Color.rgb(255, 248, 247), 23f)
                val heading = LinearLayout(this@MainActivity).apply { gravity = Gravity.CENTER_VERTICAL }
                heading.addView(iconBadge(R.drawable.ic_bell_outline,
                    if (overdue) Color.rgb(204, 79, 65) else blue,
                    if (overdue) Color.rgb(255, 238, 235) else Color.rgb(236, 244, 255)))
                heading.addView(LinearLayout(this@MainActivity).apply {
                    orientation = LinearLayout.VERTICAL
                    addView(sectionTitle(item.optString("lead_name")).apply {
                        setPadding(0, 0, 0, 0); maxLines = 1; ellipsize = android.text.TextUtils.TruncateAt.END
                    })
                    addView(body(if (overdue) "Overdue · ${formatDate(item.optString("due_at"))}" else formatDate(item.optString("due_at"))).apply {
                        setTextColor(if (overdue) Color.rgb(204, 79, 65) else Color.rgb(35, 139, 100))
                        setPadding(0, dp(4), 0, 0)
                    })
                }, LinearLayout.LayoutParams(0, -2, 1f).apply { leftMargin = dp(14) })
                if (item.optString("status") != "completed") {
                    val deleteControl = iconAction(R.drawable.ic_trash_outline, "Delete reminder").apply {
                        imageTintList = ColorStateList.valueOf(Color.rgb(204, 79, 65))
                        background = RippleDrawable(ColorStateList.valueOf(Color.rgb(255, 220, 216)),
                            rounded(Color.rgb(255, 238, 235), 17f), null)
                    }
                    deleteControl.setOnClickListener {
                        AlertDialog.Builder(this@MainActivity).setTitle("Delete reminder?")
                            .setMessage(item.optString("lead_name")).setNegativeButton("Cancel", null)
                            .setPositiveButton("Delete") { _, _ -> reminderAction(item, "delete", deleteControl) }.show()
                    }
                    heading.addView(deleteControl)
                }
                row.addView(heading)
                row.addView(space(20))
                row.addView(sectionTitle(item.optString("title")).apply { setPadding(0, 0, 0, 0) })
                if (item.optString("description").isNotBlank()) row.addView(body(item.optString("description")).apply {
                    setPadding(0, dp(8), 0, 0)
                })
                row.addView(space(22))
                row.addView(divider())
                row.addView(space(18))
                if (item.optString("status") != "completed") {
                    val actions = LinearLayout(this@MainActivity)
                    actions.addView(secondaryButton("Snooze 30 min").apply { setOnClickListener { reminderAction(item, "snooze", this) } },
                        LinearLayout.LayoutParams(0, dp(50), 1f).apply { rightMargin = dp(10) })
                    actions.addView(primaryButton("Mark done").apply { setOnClickListener { reminderAction(item, "complete", this) } },
                        LinearLayout.LayoutParams(0, dp(50), 1f))
                    row.addView(actions)
                } else {
                    row.addView(body("Completed").apply { setTextColor(Color.rgb(35, 139, 100)); setPadding(0, 0, 0, 0) })
                }
                if (item.optString("phone").isNotBlank()) {
                    row.addView(space(8))
                    val contactActions = LinearLayout(this@MainActivity)
                    contactActions.addView(quietButton("Call").apply { setOnClickListener { dial(item.optString("phone")) } },
                        LinearLayout.LayoutParams(0, dp(44), 1f))
                    contactActions.addView(quietButton("WhatsApp").apply { setOnClickListener { openWhatsApp(item.optString("phone")) } },
                        LinearLayout.LayoutParams(0, dp(44), 1f))
                    row.addView(contactActions)
                }
                val leadId = item.optString("lead_id")
                if (leadId.isNotBlank()) row.setOnClickListener { showLeadDetail(leadId) }
                content.addView(row)
                content.addView(space(14))
            }
            if (rows.length() > 0) content.addView(pagination(data.optBoolean("has_next")))
        }
    }

    private fun openCallNoteAction(callId: String) {
        lifecycleScope.launch {
            val response = withContext(Dispatchers.IO) {
                runCatching {
                    ApiClient(this@MainActivity).authorizedGet(apiPath + "calls/" + callId + "/")
                }.getOrNull()
            }
            val data = if (response?.successful == true) {
                runCatching { JSONObject(response.body) }.getOrNull()
            } else null
            val call = data?.optJSONObject("call")
            if (call != null) {
                editCallNotes(call)
            } else {
                toast("Could not open this call note.")
                showCallDetail(callId)
            }
        }
    }

    private fun showCallDetail(callId: String) {
        if (callId.isBlank()) return
        val content = dialogContent()
        val scroll = ScrollView(this).apply { addView(content) }
        val dialog = AlertDialog.Builder(this)
            .setTitle("Call detail")
            .setView(scroll)
            .setNegativeButton("Close", null)
            .create()
        content.addView(body("Loading call details…"))
        dialog.show()
        lifecycleScope.launch {
            val response = withContext(Dispatchers.IO) {
                runCatching { ApiClient(this@MainActivity).authorizedGet(apiPath + "calls/" + callId + "/") }.getOrNull()
            }
            content.removeAllViews()
            if (response?.successful != true) {
                content.addView(body("Could not load this call. Check your connection and retry."))
                return@launch
            }
            val data = runCatching { JSONObject(response.body) }.getOrNull()
            if (data == null) {
                content.addView(body("SHVYA returned an invalid call response."))
                return@launch
            }
            val call = data.getJSONObject("call")
            val lead = call.optJSONObject("lead")
            val name = call.optString("contact_name").ifBlank { lead?.optString("name").orEmpty() }
                .ifBlank { call.optString("phone_number") }

            content.addView(kicker("CALL"))
            content.addView(title(name).apply { textSize = 28f })
            content.addView(body(call.optString("phone_number")))
            content.addView(space(14))
            val meta = card()
            meta.addView(sectionTitle(
                call.optString("direction").replaceFirstChar { it.uppercase() } + " · " +
                    call.optString("status").replace('_', ' ').replaceFirstChar { it.uppercase() }
            ))
            meta.addView(body(
                formatDate(call.optString("ended_at")) + " · Talk " +
                    call.optString("talk_duration").ifBlank { call.optInt("talk_duration_seconds").toString() + " sec" }
            ))
            meta.addView(space(10))
            meta.addView(body("Ring " + call.optString("ring_duration").ifBlank { "—" }))
            content.addView(meta)
            content.addView(space(12))

            val contactActions = LinearLayout(this@MainActivity)
            contactActions.addView(secondaryButton("Call back").apply {
                setOnClickListener { dial(call.optString("phone_number")) }
            }, LinearLayout.LayoutParams(0, dp(48), 1f).apply { rightMargin = dp(8) })
            contactActions.addView(primaryButton("WhatsApp").apply {
                setOnClickListener { openWhatsApp(call.optString("phone_number")) }
            }, LinearLayout.LayoutParams(0, dp(48), 1f))
            content.addView(contactActions)

            content.addView(space(22))
            content.addView(sectionTitle("CRM lead").apply { textSize = 20f })
            if (lead != null) {
                val leadBox = card()
                leadBox.addView(sectionTitle(lead.optString("name")).apply { setPadding(0, 0, 0, 0) })
                leadBox.addView(body(lead.optString("pipeline") + "  ›  " + lead.optString("stage")))
                if (lead.optString("source").isNotBlank()) {
                    leadBox.addView(body("Source · " + lead.optString("source").replace('_', ' ')))
                }
                leadBox.setOnClickListener {
                    dialog.dismiss()
                    showLeadDetail(lead.optString("id"))
                }
                content.addView(leadBox)
                content.addView(space(8))
                content.addView(secondaryButton("Move stage").apply {
                    setOnClickListener {
                        dialog.dismiss()
                        showLeadDetail(lead.optString("id"))
                    }
                })
            } else {
                content.addView(body("This number is not linked to a SHVYA CRM lead yet."))
                content.addView(space(10))
                content.addView(primaryButton("Create CRM lead").apply {
                    setOnClickListener {
                        leadPrefillName = call.optString("contact_name")
                        leadPrefillPhone = call.optString("phone_number")
                        dialog.dismiss()
                        addLead()
                    }
                })
            }

            content.addView(space(22))
            content.addView(sectionTitle("Outcome").apply { textSize = 20f })
            content.addView(body("Classify what happened so missed calls and follow-ups stay actionable."))
            content.addView(space(10))
            val dispositions = data.optJSONArray("dispositions") ?: org.json.JSONArray()
            val outcomeButton = secondaryButton(
                dispositionLabel(dispositions, call.optString("disposition")).ifBlank { "Choose outcome  ⌄" }
            )
            outcomeButton.setOnClickListener {
                if (dispositions.length() == 0) {
                    toast("No call outcomes are configured for this organization.")
                    return@setOnClickListener
                }
                val labels = Array(dispositions.length()) { index -> dispositions.getJSONObject(index).optString("name") }
                AlertDialog.Builder(this@MainActivity).setTitle("Call outcome").setItems(labels) { _, which ->
                    val selected = dispositions.getJSONObject(which)
                    saveCallOutcome(call, selected.optString("code"), outcomeButton, selected.optString("name"))
                }.show()
            }
            content.addView(outcomeButton)

            content.addView(space(22))
            content.addView(sectionTitle("Quick notes").apply { textSize = 20f })
            content.addView(body("Tap a common note or write your own."))
            val quickNotes = listOf(
                "Interested", "Send pricing", "Call tomorrow",
                "Requested demo", "Not interested", "No answer",
            )
            quickNotes.chunked(2).forEach { pair ->
                val noteRow = LinearLayout(this@MainActivity)
                pair.forEach { text ->
                    noteRow.addView(quietButton(text).apply {
                        setOnClickListener { saveQuickNote(call, text) }
                    }, LinearLayout.LayoutParams(0, dp(42), 1f))
                }
                content.addView(noteRow)
            }
            if (call.optString("notes").isNotBlank()) {
                content.addView(space(10))
                content.addView(card().apply {
                    addView(body(call.optString("notes")).apply { setTextColor(ink); setPadding(0, 0, 0, 0) })
                })
            }
            content.addView(space(10))
            content.addView(secondaryButton(if (call.optString("notes").isBlank()) "Add detailed note" else "Edit detailed note").apply {
                setOnClickListener { editCallNotes(call) }
            })

            content.addView(space(22))
            content.addView(sectionTitle("Next follow-up").apply { textSize = 20f })
            if (lead == null) {
                content.addView(body("Create or link a CRM lead before setting a follow-up."))
            } else {
                if (call.optBoolean("follow_up_required") && call.optString("follow_up_at").isNotBlank()) {
                    content.addView(body("Scheduled · " + formatDate(call.optString("follow_up_at"))).apply {
                        setTextColor(Color.rgb(35, 139, 100))
                    })
                    content.addView(space(8))
                }
                val followRow = LinearLayout(this@MainActivity)
                followRow.addView(quietButton("1 hour").apply {
                    setOnClickListener { scheduleCallFollowUp(callId, System.currentTimeMillis() + 60L * 60L * 1000L) }
                }, LinearLayout.LayoutParams(0, dp(42), 1f))
                followRow.addView(quietButton("Tomorrow").apply {
                    setOnClickListener { scheduleCallFollowUp(callId, tomorrowAtTen()) }
                }, LinearLayout.LayoutParams(0, dp(42), 1f))
                followRow.addView(quietButton("3 days").apply {
                    setOnClickListener { scheduleCallFollowUp(callId, daysFromNowAtTen(3)) }
                }, LinearLayout.LayoutParams(0, dp(42), 1f))
                content.addView(followRow)
                content.addView(quietButton("Custom date & time").apply {
                    setOnClickListener { pickFollowUpDate { value -> scheduleCallFollowUp(callId, value) } }
                })
            }

            val history = data.optJSONArray("history") ?: org.json.JSONArray()
            if (history.length() > 0) {
                content.addView(space(22))
                content.addView(sectionTitle("Previous calls").apply { textSize = 20f })
                content.addView(body("Recent call history for this contact."))
                content.addView(space(10))
                for (i in 0 until history.length()) {
                    content.addView(compactCallCard(history.getJSONObject(i)))
                    content.addView(space(8))
                }
            }
        }
    }

    private fun showLeadDetail(leadId: String) {
        if (leadId.isBlank()) return
        leadDetailId = leadId
        render()
    }

    private fun showLeadDetailPage(leadId: String) {
        val version = ++renderVersion
        val shell = LinearLayout(this).apply {
            orientation = LinearLayout.VERTICAL
            setBackgroundColor(canvas)
        }
        val header = LinearLayout(this).apply {
            gravity = Gravity.CENTER_VERTICAL
            minimumHeight = dp(58)
            setPadding(dp(14), dp(6), dp(14), dp(6))
            setBackgroundColor(Color.WHITE)
        }
        header.addView(quietButton("‹  Back").apply {
            setOnClickListener {
                leadDetailId = ""
                render()
            }
        }, LinearLayout.LayoutParams(dp(82), dp(44)))
        header.addView(sectionTitle("Lead details").apply {
            gravity = Gravity.CENTER
            textSize = 17f
            setPadding(0, 0, 0, 0)
        }, LinearLayout.LayoutParams(0, -2, 1f))
        header.addView(View(this), LinearLayout.LayoutParams(dp(82), dp(1)))
        shell.addView(header)
        shell.addView(divider())

        val content = page().apply {
            setPadding(dp(20), dp(22), dp(20), dp(34))
        }
        content.addView(body("Loading lead…"))
        val scroll = ScrollView(this).apply {
            isFillViewport = true
            clipToPadding = false
            addView(content)
        }
        shell.addView(scroll, LinearLayout.LayoutParams(-1, 0, 1f))
        ViewCompat.setOnApplyWindowInsetsListener(shell) { view, insets ->
            val bars = safeInsets(insets)
            view.setPadding(bars.left, bars.top, bars.right, bars.bottom)
            insets
        }
        setContentView(shell)

        lifecycleScope.launch {
            val response = withContext(Dispatchers.IO) {
                runCatching {
                    ApiClient(this@MainActivity).authorizedGet(apiPath + "leads/" + leadId + "/")
                }.getOrNull()
            }
            if (version != renderVersion || leadDetailId != leadId || isFinishing) return@launch
            content.removeAllViews()

            if (response?.successful != true) {
                content.addView(emptyCard(
                    "Could not load lead",
                    "Check your connection and try again."
                ))
                content.addView(space(12))
                content.addView(secondaryButton("Retry").apply {
                    setOnClickListener { showLeadDetailPage(leadId) }
                })
                return@launch
            }

            val payload = runCatching { JSONObject(response.body) }.getOrNull()
            val lead = payload?.optJSONObject("lead")
            if (lead == null) {
                content.addView(emptyCard("Lead unavailable", "SHVYA returned an invalid lead response."))
                return@launch
            }

            val isFavorite = favoriteLeadIds().contains(leadId)
            val phone = lead.optString("phone")
            val email = lead.optString("email")
            val pipeline = lead.optString("pipeline")
            val stage = lead.optString("stage")

            content.addView(kicker("CRM LEAD"))
            content.addView(title(lead.optString("name").ifBlank { "Unnamed lead" }).apply {
                textSize = 30f
                setPadding(0, dp(6), 0, 0)
            })
            content.addView(body(
                listOf(phone, email).filter { it.isNotBlank() }.joinToString("  ·  ")
            ).apply { textSize = 14f })
            content.addView(space(18))

            val statusCard = card()
            statusCard.addView(body("PIPELINE").apply {
                textSize = 11f
                setTypeface(typeface, Typeface.BOLD)
                setTextColor(muted)
                setPadding(0, 0, 0, dp(5))
            })
            statusCard.addView(sectionTitle(pipeline.ifBlank { "No pipeline" }).apply {
                textSize = 18f
                setPadding(0, 0, 0, 0)
            })
            statusCard.addView(body(stage.ifBlank { "No stage" }).apply {
                setTextColor(blue)
                setTypeface(typeface, Typeface.BOLD)
                setPadding(0, dp(4), 0, 0)
            })
            val source = lead.optString("source").replace('_', ' ').trim()
            if (source.isNotBlank()) {
                statusCard.addView(space(10))
                statusCard.addView(body("Source · " + source.replaceFirstChar { it.uppercase() }).apply {
                    setPadding(0, 0, 0, 0)
                })
            }
            content.addView(statusCard)
            content.addView(space(12))

            val contactActions = LinearLayout(this@MainActivity)
            contactActions.addView(primaryButton("Call").apply {
                isEnabled = phone.isNotBlank()
                setOnClickListener { dial(phone) }
            }, LinearLayout.LayoutParams(0, dp(50), 1f).apply { rightMargin = dp(8) })
            contactActions.addView(secondaryButton("WhatsApp").apply {
                isEnabled = phone.isNotBlank()
                setOnClickListener { openWhatsApp(phone) }
            }, LinearLayout.LayoutParams(0, dp(50), 1f))
            content.addView(contactActions)
            content.addView(space(8))

            val manageActions = LinearLayout(this@MainActivity)
            manageActions.addView(secondaryButton("Move stage").apply {
                setOnClickListener {
                    chooseLeadStage(lead) {
                        showLeadDetailPage(leadId)
                    }
                }
            }, LinearLayout.LayoutParams(0, dp(48), 1f).apply { rightMargin = dp(8) })
            manageActions.addView(priorityButton(isFavorite).apply {
                setOnClickListener {
                    val active = toggleFavoriteLead(leadId)
                    toast(if (active) "Added to priority leads" else "Removed from priority leads")
                    showLeadDetailPage(leadId)
                }
            }, LinearLayout.LayoutParams(0, dp(48), 1f))
            content.addView(manageActions)

            content.addView(space(26))
            content.addView(sectionTitle("Follow-up").apply { textSize = 19f })
            val reminder = lead.optJSONObject("reminder")
            val followCard = card()
            if (reminder != null) {
                val overdue = reminder.optBoolean("overdue")
                followCard.addView(sectionTitle(
                    if (overdue) "Follow-up overdue" else "Follow-up scheduled"
                ).apply {
                    setTextColor(if (overdue) Color.rgb(196, 58, 54) else Color.rgb(35, 139, 100))
                    setPadding(0, 0, 0, 0)
                })
                followCard.addView(body(formatDate(reminder.optString("due_at"))))
                if (reminder.optString("title").isNotBlank()) {
                    followCard.addView(body(reminder.optString("title")).apply { setTextColor(ink) })
                }
                followCard.addView(space(14))
            } else {
                followCard.addView(body("No follow-up is scheduled.").apply {
                    setPadding(0, 0, 0, dp(12))
                })
            }
            val followRow = LinearLayout(this@MainActivity)
            followRow.addView(quietButton("1 hour").apply {
                setOnClickListener {
                    scheduleLeadFollowUp(leadId, System.currentTimeMillis() + 60L * 60L * 1000L)
                }
            }, LinearLayout.LayoutParams(0, dp(42), 1f))
            followRow.addView(quietButton("Tomorrow").apply {
                setOnClickListener { scheduleLeadFollowUp(leadId, tomorrowAtTen()) }
            }, LinearLayout.LayoutParams(0, dp(42), 1f))
            followRow.addView(quietButton("3 days").apply {
                setOnClickListener { scheduleLeadFollowUp(leadId, daysFromNowAtTen(3)) }
            }, LinearLayout.LayoutParams(0, dp(42), 1f))
            followCard.addView(followRow)
            followCard.addView(quietButton("Choose date & time").apply {
                setOnClickListener {
                    pickFollowUpDate { value -> scheduleLeadFollowUp(leadId, value) }
                }
            })
            content.addView(followCard)

            val visibleAttributes = humanLeadAttributes(lead)
            if (visibleAttributes.isNotEmpty()) {
                content.addView(space(26))
                content.addView(sectionTitle("Lead information").apply { textSize = 19f })
                content.addView(space(10))
                val detailsCard = card()
                visibleAttributes.forEachIndexed { index, row ->
                    detailsCard.addView(detailRow(row.first, row.second))
                    if (index != visibleAttributes.lastIndex) detailsCard.addView(divider())
                }
                content.addView(detailsCard)
            }

            val notes = lead.optString("notes").trim()
            if (notes.isNotBlank()) {
                content.addView(space(26))
                content.addView(sectionTitle("Notes").apply { textSize = 19f })
                content.addView(card().apply {
                    addView(body(notes).apply {
                        setTextColor(ink)
                        setPadding(0, 0, 0, 0)
                    })
                })
            }

            val calls = lead.optJSONArray("recent_calls") ?: org.json.JSONArray()
            content.addView(space(26))
            content.addView(sectionTitle("Call history").apply { textSize = 19f })
            if (calls.length() == 0) {
                content.addView(emptyCard("No calls yet", "Calls linked to this lead will appear here."))
            } else {
                content.addView(space(10))
                for (i in 0 until calls.length()) {
                    content.addView(compactCallCard(calls.getJSONObject(i)))
                    if (i < calls.length() - 1) content.addView(space(8))
                }
            }

            content.addView(space(24))
            content.addView(quietButton("Open in web CRM").apply {
                setOnClickListener {
                    startActivity(Intent(
                        Intent.ACTION_VIEW,
                        Uri.parse(BuildConfig.SHVYA_BASE_URL.trimEnd('/') + "/dashboard/")
                    ))
                }
            })
        }
    }

    private fun dispositionLabel(rows: org.json.JSONArray, code: String): String {
        if (code.isBlank()) return ""
        for (i in 0 until rows.length()) {
            val row = rows.optJSONObject(i) ?: continue
            if (row.optString("code") == code) return row.optString("name")
        }
        return code.replace('_', ' ').replaceFirstChar { it.uppercase() }
    }

    private fun saveCallOutcome(call: JSONObject, code: String, button: Button, label: String) {
        button.isEnabled = false
        lifecycleScope.launch {
            val path = apiPath + "calls/" + call.getString("id") + "/notes/"
            val payload = JSONObject()
                .put("notes", call.optString("notes"))
                .put("disposition", code)
            val result = withContext(Dispatchers.IO) {
                runCatching { ApiClient(this@MainActivity).authorizedPatch(path, payload) }.getOrNull()
            }
            button.isEnabled = true
            if (result?.successful == true) {
                call.put("disposition", code)
                button.text = label
                toast("Call outcome saved")
                render()
            } else if (shouldQueueOffline(result)) {
                queueOfflineAction(path, "PATCH", payload, "Call outcome")
                call.put("disposition", code)
                button.text = label
                toast("Saved offline · will sync automatically")
            } else toast(apiError(result?.body, "Could not save call outcome."))
        }
    }

    private fun saveQuickNote(call: JSONObject, value: String) {
        val existing = call.optString("notes").trim()
        val notes = if (existing.isBlank()) value else existing + "\n" + value
        lifecycleScope.launch {
            val path = apiPath + "calls/" + call.getString("id") + "/notes/"
            val payload = JSONObject()
                .put("notes", notes)
                .put("disposition", call.optString("disposition"))
            val result = withContext(Dispatchers.IO) {
                runCatching { ApiClient(this@MainActivity).authorizedPatch(path, payload) }.getOrNull()
            }
            if (result?.successful == true) {
                call.put("notes", notes)
                toast("Quick note saved")
                render()
            } else if (shouldQueueOffline(result)) {
                queueOfflineAction(path, "PATCH", payload, "Call note")
                call.put("notes", notes)
                toast("Saved offline · will sync automatically")
            } else toast(apiError(result?.body, "Could not save note."))
        }
    }

    private fun scheduleCallFollowUp(callId: String, dueAtMillis: Long) {
        lifecycleScope.launch {
            val path = apiPath + "calls/" + callId + "/follow-up/"
            val payload = JSONObject()
                .put("due_at", isoUtc(dueAtMillis))
                .put("title", "Call follow-up")
            val response = withContext(Dispatchers.IO) {
                runCatching { ApiClient(this@MainActivity).authorizedPost(path, payload) }.getOrNull()
            }
            if (response?.successful == true) {
                toast("Follow-up scheduled")
                selectedTab = "followups"
                reminderSegment = "today"
                render()
            } else if (shouldQueueOffline(response)) {
                queueOfflineAction(path, "POST", payload, "Call follow-up")
                toast("Follow-up saved offline · will sync automatically")
            } else toast(apiError(response?.body, "Could not create follow-up."))
        }
    }

    private fun scheduleLeadFollowUp(leadId: String, dueAtMillis: Long) {
        lifecycleScope.launch {
            val path = apiPath + "leads/" + leadId + "/"
            val payload = JSONObject()
                .put("due_at", isoUtc(dueAtMillis))
                .put("title", "Lead follow-up")
            val response = withContext(Dispatchers.IO) {
                runCatching { ApiClient(this@MainActivity).authorizedPost(path, payload) }.getOrNull()
            }
            if (response?.successful == true) {
                toast("Follow-up scheduled")
                render()
            } else if (shouldQueueOffline(response)) {
                queueOfflineAction(path, "POST", payload, "Lead follow-up")
                toast("Follow-up saved offline · will sync automatically")
            } else toast(apiError(response?.body, "Could not create follow-up."))
        }
    }

    private fun chooseLeadStage(lead: JSONObject, onMoved: () -> Unit = { render() }) {
        val pipelines = lead.optJSONArray("pipelines") ?: return
        val labels = mutableListOf<String>()
        val pipelineIds = mutableListOf<String>()
        val stageIds = mutableListOf<String>()
        for (i in 0 until pipelines.length()) {
            val pipeline = pipelines.getJSONObject(i)
            val stages = pipeline.optJSONArray("stages") ?: continue
            for (j in 0 until stages.length()) {
                val stage = stages.getJSONObject(j)
                labels.add(pipeline.optString("name") + "  ›  " + stage.optString("name"))
                pipelineIds.add(pipeline.optString("id"))
                stageIds.add(stage.optString("id"))
            }
        }
        if (labels.isEmpty()) {
            toast("No stages are available.")
            return
        }
        AlertDialog.Builder(this)
            .setTitle("Move lead")
            .setItems(labels.toTypedArray()) { _, which ->
                lifecycleScope.launch {
                    val path = apiPath + "leads/" + lead.getString("id") + "/"
                    val payload = JSONObject()
                        .put("pipeline_id", pipelineIds[which])
                        .put("stage_id", stageIds[which])
                    val response = withContext(Dispatchers.IO) {
                        runCatching { ApiClient(this@MainActivity).authorizedPatch(path, payload) }.getOrNull()
                    }
                    if (response?.successful == true) {
                        toast("Lead moved")
                        onMoved()
                    } else if (shouldQueueOffline(response)) {
                        queueOfflineAction(path, "PATCH", payload, "Move lead")
                        toast("Stage change saved offline · will sync automatically")
                        onMoved()
                    } else toast(apiError(response?.body, "Could not move this lead."))
                }
            }
            .show()
    }

    private fun pickFollowUpDate(chosen: (Long) -> Unit) {
        val now = Calendar.getInstance()
        DatePickerDialog(this, { _, year, month, day ->
            TimePickerDialog(this, { _, hour, minute ->
                val value = Calendar.getInstance().apply {
                    set(Calendar.YEAR, year)
                    set(Calendar.MONTH, month)
                    set(Calendar.DAY_OF_MONTH, day)
                    set(Calendar.HOUR_OF_DAY, hour)
                    set(Calendar.MINUTE, minute)
                    set(Calendar.SECOND, 0)
                    set(Calendar.MILLISECOND, 0)
                }
                if (value.timeInMillis <= System.currentTimeMillis()) {
                    toast("Choose a future time.")
                } else chosen(value.timeInMillis)
            }, now.get(Calendar.HOUR_OF_DAY), now.get(Calendar.MINUTE), false).show()
        }, now.get(Calendar.YEAR), now.get(Calendar.MONTH), now.get(Calendar.DAY_OF_MONTH)).show()
    }

    private fun tomorrowAtTen(): Long = daysFromNowAtTen(1)

    private fun daysFromNowAtTen(days: Int): Long = Calendar.getInstance().apply {
        add(Calendar.DAY_OF_YEAR, days)
        set(Calendar.HOUR_OF_DAY, 10)
        set(Calendar.MINUTE, 0)
        set(Calendar.SECOND, 0)
        set(Calendar.MILLISECOND, 0)
    }.timeInMillis

    private fun isoUtc(value: Long): String = SimpleDateFormat(
        "yyyy-MM-dd'T'HH:mm:ss.SSS'Z'",
        Locale.US,
    ).apply { timeZone = TimeZone.getTimeZone("UTC") }.format(Date(value))

    private fun openWhatsApp(phone: String) {
        val digits = phone.filter { it.isDigit() }
        if (digits.isBlank()) {
            toast("No phone number is available.")
            return
        }
        runCatching {
            startActivity(Intent(Intent.ACTION_VIEW, Uri.parse("https://wa.me/" + digits)))
        }.onFailure { toast("WhatsApp could not be opened.") }
    }

    private fun favoriteLeadIds(): Set<String> {
        val prefs = getSharedPreferences("shvya_mobile_ui", MODE_PRIVATE)
        return prefs.getStringSet("favorite_leads", emptySet())?.toSet() ?: emptySet()
    }

    private fun toggleFavoriteLead(leadId: String): Boolean {
        val prefs = getSharedPreferences("shvya_mobile_ui", MODE_PRIVATE)
        val values = favoriteLeadIds().toMutableSet()
        val nowFavorite = if (values.contains(leadId)) {
            values.remove(leadId)
            false
        } else {
            values.add(leadId)
            true
        }
        prefs.edit().putStringSet("favorite_leads", values).apply()
        return nowFavorite
    }

    private fun recentLeadSearches(): List<String> {
        val prefs = getSharedPreferences("shvya_mobile_ui", MODE_PRIVATE)
        return prefs.getString("recent_lead_searches", "").orEmpty()
            .split("|||")
            .filter { it.isNotBlank() }
            .take(5)
    }

    private fun rememberLeadSearch(value: String) {
        if (value.isBlank()) return
        val items = recentLeadSearches().toMutableList()
        items.remove(value)
        items.add(0, value)
        getSharedPreferences("shvya_mobile_ui", MODE_PRIVATE)
            .edit()
            .putString("recent_lead_searches", items.take(5).joinToString("|||"))
            .apply()
    }

    private fun apiError(bodyText: String?, fallback: String): String {
        if (bodyText.isNullOrBlank()) return fallback
        return runCatching { JSONObject(bodyText).optString("detail") }.getOrNull()
            .orEmpty()
            .ifBlank { fallback }
    }

    private fun shouldQueueOffline(response: com.shvya.callintelligence.net.ApiResponse?): Boolean =
        response == null || response.code == 408 || response.code == 429 || response.code in 500..599

    private suspend fun queueOfflineAction(
        path: String,
        method: String,
        payload: JSONObject,
        label: String,
    ) {
        withContext(Dispatchers.IO) {
            AppDatabase.get(this@MainActivity).callDao().insertPendingAction(
                PendingAction(
                    path = path,
                    method = method,
                    payload = payload.toString(),
                    label = label,
                )
            )
        }
        pendingSyncCount += 1
        TrackingScheduler.enqueueSync(this)
    }

    private fun performReminderAction(reminderId: String, action: String) {
        lifecycleScope.launch {
            val path = apiPath + "reminders/" + reminderId + "/action/"
            val payload = JSONObject().put("action", action)
            val response = withContext(Dispatchers.IO) {
                runCatching {
                    ApiClient(this@MainActivity).authorizedPost(path, payload)
                }.getOrNull()
            }
            if (response?.successful == true) {
                toast(if (action == "complete") "Follow-up completed" else "Follow-up snoozed 30 minutes")
                render()
            } else if (shouldQueueOffline(response)) {
                queueOfflineAction(path, "POST", payload, "Follow-up " + action)
                toast("Follow-up action saved offline · will sync automatically")
            } else toast(apiError(response?.body, "Could not update the follow-up."))
        }
    }

    private fun reminderAction(item: JSONObject, action: String, button: View) {
        button.isEnabled = false
        lifecycleScope.launch {
            val path = apiPath + "reminders/" + item.getString("id") + "/action/"
            val payload = JSONObject().put("action", action)
            val response = withContext(Dispatchers.IO) {
                runCatching { ApiClient(this@MainActivity).authorizedPost(path, payload) }.getOrNull()
            }
            if (response?.successful == true) {
                render()
            } else if (shouldQueueOffline(response)) {
                queueOfflineAction(path, "POST", payload, "Follow-up " + action)
                toast("Saved offline · will sync automatically")
                render()
            } else {
                button.isEnabled = true
                toast(apiError(response?.body, "Could not update the reminder. Please retry."))
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
                    val path = apiPath + "calls/" + call.getString("id") + "/notes/"
                    val payload = JSONObject()
                        .put("notes", notes.text.toString().trim())
                        .put("disposition", call.optString("disposition"))
                    val result = withContext(Dispatchers.IO) {
                        runCatching { ApiClient(this@MainActivity).authorizedPatch(path, payload) }.getOrNull()
                    }
                    if (result?.successful == true) {
                        dialog.dismiss()
                        toast("Call notes saved to CRM")
                        render()
                    } else if (shouldQueueOffline(result)) {
                        queueOfflineAction(path, "PATCH", payload, "Call notes")
                        dialog.dismiss()
                        toast("Note saved offline · will sync automatically")
                    } else {
                        button.isEnabled = true
                        message.text = apiError(result?.body, "Could not save notes. Please retry.")
                    }
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
            } else content.addView(body("Could not load organization settings. You can still manage this device below."))

            content.addView(space(24))
            content.addView(sectionTitle("Device & sync"))
            val deviceCard = card()
            deviceCard.addView(statusLine("Call access", essentialPermissionsGranted()))
            deviceCard.addView(statusLine(
                if (pendingSyncCount == 0) "All activity synced" else pendingSyncCount.toString() + " items waiting to sync",
                pendingSyncCount == 0,
            ))
            deviceCard.addView(space(10))
            deviceCard.addView(secondaryButton("Sync now").apply {
                setOnClickListener {
                    TrackingScheduler.enqueueReconcile(this@MainActivity)
                    TrackingScheduler.enqueueSync(this@MainActivity)
                    toast("Sync requested")
                    refreshLocalSyncHealth()
                }
            })
            content.addView(deviceCard)
            content.addView(space(10))
            val deviceActions = LinearLayout(this@MainActivity)
            deviceActions.addView(quietButton("Permissions").apply {
                setOnClickListener { permissionLauncher.launch(requiredPermissions()) }
            }, LinearLayout.LayoutParams(0, dp(44), 1f).apply { rightMargin = dp(8) })
            deviceActions.addView(quietButton("Background access").apply {
                setOnClickListener { requestBatteryOptimizationExemption() }
            }, LinearLayout.LayoutParams(0, dp(44), 1f))
            content.addView(deviceActions)

            content.addView(space(24))
            content.addView(sectionTitle("Account"))
            content.addView(body(auth.email.ifBlank { "Signed in to SHVYA" }).apply {
                setTextColor(ink)
                setPadding(0, dp(6), 0, dp(10))
            })
            content.addView(secondaryButton("Open web dashboard").apply {
                setOnClickListener {
                    startActivity(Intent(Intent.ACTION_VIEW, Uri.parse(
                        BuildConfig.SHVYA_BASE_URL.trimEnd('/') + "/dashboard/call-intelligence/?section=analytics"
                    )))
                }
            })
            content.addView(body("App version " + BuildConfig.VERSION_NAME).apply {
                textSize = 12f
                setPadding(0, dp(10), 0, dp(6))
            })
            content.addView(quietButton("Sign out").apply { setOnClickListener {
                AlertDialog.Builder(this@MainActivity).setTitle("Sign out?")
                    .setMessage("Call tracking will stop on this device until you sign in again.")
                    .setNegativeButton("Cancel", null).setPositiveButton("Sign out") { _, _ ->
                        stopService(Intent(this@MainActivity, CallTrackingService::class.java))
                        WorkManager.getInstance(this@MainActivity).cancelAllWork()
                        auth.clear(); dialog.dismiss(); render()
                    }.show()
            } })
        }
    }

    private fun addLead() {
        leadFormOpen = true
        showLeadForm()
    }

    private fun showLeadForm() {
        val version = ++renderVersion
        val shell = LinearLayout(this).apply {
            orientation = LinearLayout.VERTICAL
            setBackgroundColor(canvas)
        }
        val header = LinearLayout(this).apply {
            gravity = Gravity.CENTER_VERTICAL
            setPadding(dp(22), dp(10), dp(22), dp(10))
            setBackgroundColor(Color.WHITE)
        }
        header.addView(quietButton("‹  Back").apply {
            setOnClickListener {
                leadFormOpen = false
                leadPrefillName = ""
                leadPrefillPhone = ""
                render()
            }
        })
        header.addView(sectionTitle("Add lead").apply {
            textSize = 18f; gravity = Gravity.CENTER; setPadding(0, 0, 0, 0)
        }, LinearLayout.LayoutParams(0, -2, 1f))
        header.addView(View(this), LinearLayout.LayoutParams(dp(66), dp(1)))
        shell.addView(header)
        shell.addView(divider())

        val content = page()
        content.addView(body("Loading your organization's lead form…"))
        shell.addView(ScrollView(this).apply { addView(content) }, LinearLayout.LayoutParams(-1, 0, 1f))
        shell.addView(divider())
        val footer = LinearLayout(this).apply {
            orientation = LinearLayout.VERTICAL
            setPadding(dp(22), dp(12), dp(22), dp(12))
            setBackgroundColor(Color.WHITE)
        }
        val submit = primaryButton("Create lead").apply { isEnabled = false }
        footer.addView(submit)
        shell.addView(footer)
        ViewCompat.setOnApplyWindowInsetsListener(shell) { view, insets ->
            val bars = safeInsets(insets)
            view.setPadding(bars.left, bars.top, bars.right, bars.bottom)
            insets
        }
        setContentView(shell)
        lifecycleScope.launch {
            val response = withContext(Dispatchers.IO) {
                runCatching { ApiClient(this@MainActivity).authorizedGet(apiPath + "leads/") }.getOrNull()
            }
            if (version != renderVersion || !leadFormOpen || isFinishing) return@launch
            content.removeAllViews()
            val data = if (response?.successful == true) runCatching { JSONObject(response.body) }.getOrNull() else null
            if (data == null) {
                content.addView(emptyCard("Form unavailable", "Check your connection and try again."))
                content.addView(space(12))
                content.addView(secondaryButton("Retry").apply { setOnClickListener { showLeadForm() } })
            } else {
                buildLeadForm(content, data, submit)
            }
        }
    }

    private fun leadField(container: LinearLayout, label: String, input: EditText) {
        container.addView(fieldLabel(label))
        container.addView(space(8))
        container.addView(input)
        container.addView(space(20))
    }

    private fun buildLeadForm(content: LinearLayout, data: JSONObject, submit: Button) {
        val pipelines = data.optJSONArray("pipelines") ?: org.json.JSONArray()
        content.addView(kicker(data.optString("organization_name").uppercase(Locale.getDefault())))
        content.addView(title("New lead").apply { textSize = 34f })
        content.addView(body("Add the details your team needs to follow up."))
        content.addView(space(25))
        if (pipelines.length() == 0) {
            content.addView(emptyCard("No pipeline available", "Ask your organization admin to assign an active pipeline before creating leads."))
            return
        }

        val details = card()
        details.addView(sectionTitle("Contact details").apply { textSize = 20f })
        details.addView(space(18))
        val name = field("Lead name").apply { setText(leadPrefillName) }
        val phone = field("+91 98765 43210").apply {
            inputType = InputType.TYPE_CLASS_PHONE
            setText(leadPrefillPhone)
        }
        val email = field("Email address").apply { inputType = InputType.TYPE_CLASS_TEXT or InputType.TYPE_TEXT_VARIATION_EMAIL_ADDRESS }
        leadField(details, "Name *", name)
        leadField(details, "Phone number *", phone)
        leadField(details, "Email", email)
        content.addView(details)
        content.addView(space(16))

        val routing = card()
        routing.addView(sectionTitle("Pipeline & stage").apply { textSize = 20f })
        routing.addView(space(18))
        var pipelineIndex = (0 until pipelines.length()).firstOrNull {
            pipelines.getJSONObject(it).optString("id") == data.optString("default_pipeline_id")
        } ?: 0
        var stageIndex = 0
        val pipelineButton = secondaryButton("")
        val stageButton = secondaryButton("")
        fun refreshSelection() {
            val pipeline = pipelines.getJSONObject(pipelineIndex)
            pipelineButton.text = pipeline.optString("name") + "  ⌄"
            val stages = pipeline.optJSONArray("stages") ?: org.json.JSONArray()
            stageButton.isEnabled = stages.length() > 0
            stageButton.text = if (stages.length() > 0) stages.getJSONObject(stageIndex).optString("name") + "  ⌄" else "No active stages"
        }
        refreshSelection()
        pipelineButton.setOnClickListener {
            val names = Array(pipelines.length()) { pipelines.getJSONObject(it).optString("name") }
            AlertDialog.Builder(this).setTitle("Choose pipeline").setItems(names) { _, index ->
                pipelineIndex = index; stageIndex = 0; refreshSelection()
            }.show()
        }
        stageButton.setOnClickListener {
            val stages = pipelines.getJSONObject(pipelineIndex).getJSONArray("stages")
            val names = Array(stages.length()) { stages.getJSONObject(it).optString("name") }
            AlertDialog.Builder(this).setTitle("Choose stage").setItems(names) { _, index ->
                stageIndex = index; refreshSelection()
            }.show()
        }
        routing.addView(fieldLabel("Pipeline *")); routing.addView(space(8)); routing.addView(pipelineButton)
        routing.addView(space(20))
        routing.addView(fieldLabel("Stage *")); routing.addView(space(8)); routing.addView(stageButton)
        content.addView(routing)
        content.addView(space(16))

        val attributeInputs = mutableMapOf<String, () -> String>()
        val definitions = data.optJSONArray("attributes") ?: org.json.JSONArray()
        if (definitions.length() > 0) {
            val attributesCard = card()
            attributesCard.addView(sectionTitle("Additional attributes").apply { textSize = 20f })
            attributesCard.addView(space(18))
            for (i in 0 until definitions.length()) {
                val definition = definitions.getJSONObject(i)
                val key = definition.getString("key")
                val label = definition.getString("name")
                when (definition.optString("field_type")) {
                    "option" -> {
                        var selected = ""
                        val options = definition.optJSONArray("options") ?: org.json.JSONArray()
                        val button = secondaryButton("Select $label  ⌄")
                        button.setOnClickListener {
                            val names = Array(options.length()) { options.getString(it) }
                            AlertDialog.Builder(this).setTitle(label).setItems(names) { _, index ->
                                selected = names[index]; button.text = selected + "  ⌄"
                            }.show()
                        }
                        attributeInputs[key] = { selected }
                        attributesCard.addView(fieldLabel(label)); attributesCard.addView(space(8)); attributesCard.addView(button)
                        attributesCard.addView(space(20))
                    }
                    "date", "datetime" -> {
                        var selected = ""
                        val button = secondaryButton("Select $label  ⌄")
                        button.setOnClickListener {
                            val now = Calendar.getInstance()
                            DatePickerDialog(this, { _, year, month, day ->
                                val date = String.format(Locale.US, "%04d-%02d-%02d", year, month + 1, day)
                                if (definition.optString("field_type") == "datetime") {
                                    TimePickerDialog(this, { _, hour, minute ->
                                        selected = String.format(Locale.US, "%sT%02d:%02d", date, hour, minute)
                                        button.text = "$date  " + String.format(Locale.US, "%02d:%02d", hour, minute)
                                    }, now.get(Calendar.HOUR_OF_DAY), now.get(Calendar.MINUTE), false).show()
                                } else { selected = date; button.text = date }
                            }, now.get(Calendar.YEAR), now.get(Calendar.MONTH), now.get(Calendar.DAY_OF_MONTH)).show()
                        }
                        attributeInputs[key] = { selected }
                        attributesCard.addView(fieldLabel(label)); attributesCard.addView(space(8)); attributesCard.addView(button)
                        attributesCard.addView(space(20))
                    }
                    else -> {
                        val input = field(label)
                        if (definition.optString("field_type") == "numeric") {
                            input.inputType = InputType.TYPE_CLASS_NUMBER or InputType.TYPE_NUMBER_FLAG_DECIMAL or InputType.TYPE_NUMBER_FLAG_SIGNED
                        }
                        attributeInputs[key] = { input.text.toString().trim() }
                        leadField(attributesCard, label, input)
                    }
                }
            }
            content.addView(attributesCard)
            content.addView(space(16))
        }
        val notesCard = card()
        notesCard.addView(sectionTitle("Notes").apply { textSize = 20f })
        notesCard.addView(space(12))
        val notes = field("What should your team know?").apply {
            minLines = 4; gravity = Gravity.TOP
            inputType = InputType.TYPE_CLASS_TEXT or InputType.TYPE_TEXT_FLAG_MULTI_LINE or InputType.TYPE_TEXT_FLAG_CAP_SENTENCES
            setPadding(dp(16), dp(14), dp(16), dp(14))
        }
        notesCard.addView(notes)
        content.addView(notesCard)
        content.addView(space(24))
        val message = body("").apply { setTextColor(Color.rgb(204, 79, 65)) }
        content.addView(message)
        submit.isEnabled = true
        submit.setOnClickListener {
            val pipeline = pipelines.getJSONObject(pipelineIndex)
            val stages = pipeline.optJSONArray("stages") ?: org.json.JSONArray()
            if (name.text.isBlank() || phone.text.isBlank() || stages.length() == 0) {
                message.text = "Enter a name and phone number, then choose an active stage."
                return@setOnClickListener
            }
            submit.isEnabled = false
            message.text = ""
            val attributes = JSONObject()
            attributeInputs.forEach { (key, value) -> attributes.put(key, value()) }
            val payload = JSONObject()
                .put("name", name.text.toString().trim())
                .put("phone", phone.text.toString().trim())
                .put("email", email.text.toString().trim())
                .put("notes", notes.text.toString().trim())
                .put("pipeline_id", pipeline.getString("id"))
                .put("stage_id", stages.getJSONObject(stageIndex).getString("id"))
                .put("attributes", attributes)
            lifecycleScope.launch {
                val response = withContext(Dispatchers.IO) {
                    runCatching { ApiClient(this@MainActivity).authorizedPost(apiPath + "leads/", payload) }.getOrNull()
                }
                if (!leadFormOpen || isFinishing) return@launch
                if (response?.successful == true) {
                    toast("Lead added to CRM")
                    leadFormOpen = false
                    leadPrefillName = ""
                    leadPrefillPhone = ""
                    selectedTab = "leads"
                    render()
                } else {
                    submit.isEnabled = true
                    message.text = if (response?.code == 409) "This number is already in your CRM."
                    else runCatching { JSONObject(response?.body ?: "").optString("detail") }.getOrNull()
                        .orEmpty().ifBlank { "Could not create lead. Check the details and retry." }
                }
            }
        }
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
            isFillViewport = true
            clipToPadding = false
            addView(content)
        }, LinearLayout.LayoutParams(-1, 0, 1f))
        shell.addView(divider())
        val nav = LinearLayout(this).apply {
            setPadding(dp(12), dp(6), dp(12), dp(6))
            setBackgroundColor(Color.WHITE)
        }
        listOf(
            Triple("today", "Today", R.drawable.ic_home_outline),
            Triple("calls", "Calls", R.drawable.ic_phone_outline),
            Triple("followups", "Follow-ups", R.drawable.ic_bell_outline),
            Triple("leads", "Leads", R.drawable.ic_people_outline),
        ).forEach { (key, label, drawable) ->
            val active = selectedTab == key
            val item = LinearLayout(this).apply {
                orientation = LinearLayout.VERTICAL
                gravity = Gravity.CENTER
                minimumHeight = dp(56)
                contentDescription = label
                background = if (active) {
                    rounded(Color.rgb(244, 248, 253), 17f)
                } else {
                    rounded(Color.TRANSPARENT, 17f)
                }
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
            val bars = safeInsets(insets)
            view.setPadding(bars.left, bars.top, bars.right, bars.bottom)
            insets
        }
        setContentView(shell)
    }

    private fun callFilterLabel(): String = when (callFilter) {
        "answered" -> "Answered"
        "missed" -> "Missed"
        "followup" -> "Needs follow-up"
        "incoming" -> "Incoming"
        "outgoing" -> "Outgoing"
        "linked" -> "CRM leads"
        "unknown" -> "No CRM lead"
        else -> "All calls"
    }

    private fun callDateLabel(): String = when {
        dateFrom.isBlank() -> "All dates"
        dateFrom == dateTo -> dateFrom
        else -> dateFrom + " – " + dateTo
    }

    private fun showCallFilterPicker() {
        val options = listOf(
            "all" to "All calls",
            "answered" to "Answered",
            "missed" to "Missed",
            "followup" to "Needs follow-up",
            "incoming" to "Incoming",
            "outgoing" to "Outgoing",
            "linked" to "CRM leads",
            "unknown" to "No CRM lead",
        )
        AlertDialog.Builder(this)
            .setTitle("Filter calls")
            .setItems(options.map { it.second }.toTypedArray()) { _, which ->
                callFilter = options[which].first
                pageNumber = 1
                render()
            }
            .show()
    }

    private fun showCallDatePicker() {
        val options = arrayOf("All dates", "Today", "Last 7 days", "Custom range")
        AlertDialog.Builder(this)
            .setTitle("Call dates")
            .setItems(options) { _, which ->
                when (which) {
                    0 -> {
                        dateFrom = ""
                        dateTo = ""
                        pageNumber = 1
                        render()
                    }
                    1 -> {
                        val value = SimpleDateFormat("yyyy-MM-dd", Locale.US).format(Date())
                        dateFrom = value
                        dateTo = value
                        pageNumber = 1
                        render()
                    }
                    2 -> {
                        val cal = Calendar.getInstance()
                        dateTo = SimpleDateFormat("yyyy-MM-dd", Locale.US).format(cal.time)
                        cal.add(Calendar.DAY_OF_YEAR, -6)
                        dateFrom = SimpleDateFormat("yyyy-MM-dd", Locale.US).format(cal.time)
                        pageNumber = 1
                        render()
                    }
                    else -> pickDate("From date") { start ->
                        pickDate("To date") { end ->
                            if (end < start) {
                                toast("End date must follow the start date.")
                            } else {
                                dateFrom = start
                                dateTo = end
                                pageNumber = 1
                                render()
                            }
                        }
                    }
                }
            }
            .show()
    }

    private fun pickDate(label: String, chosen: (String) -> Unit) {
        val now = Calendar.getInstance()
        DatePickerDialog(this, { _, y, m, d -> chosen(String.format(Locale.US, "%04d-%02d-%02d", y, m + 1, d)) }, now.get(Calendar.YEAR), now.get(Calendar.MONTH), now.get(Calendar.DAY_OF_MONTH)).apply { setTitle(label); show() }
    }

    private fun formatDurationSeconds(value: Int): String {
        val seconds = value.coerceAtLeast(0)
        val hours = seconds / 3600
        val minutes = (seconds % 3600) / 60
        return when {
            hours > 0 -> hours.toString() + "h " + minutes + "m"
            minutes > 0 -> minutes.toString() + "m"
            else -> seconds.toString() + "s"
        }
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

    private fun safeInsets(insets: WindowInsetsCompat) =
        insets.getInsets(
            WindowInsetsCompat.Type.systemBars() or
                WindowInsetsCompat.Type.displayCutout()
        )

    private fun page(): LinearLayout =
        LinearLayout(this).apply {
            orientation = LinearLayout.VERTICAL
            setPadding(dp(22), dp(18), dp(22), dp(36))
            setBackgroundColor(canvas)
        }

    private fun setPage(content: LinearLayout) {
        val scroll = ScrollView(this).apply {
            isFillViewport = true
            clipToPadding = false
            setBackgroundColor(canvas)
            addView(content)
        }
        ViewCompat.setOnApplyWindowInsetsListener(scroll) { view, insets ->
            val bars = safeInsets(insets)
            view.setPadding(bars.left, bars.top, bars.right, bars.bottom)
            insets
        }
        setContentView(scroll)
    }

    private fun card(): LinearLayout =
        LinearLayout(this).apply {
            orientation = LinearLayout.VERTICAL
            setPadding(dp(20), dp(20), dp(20), dp(20))
            background = roundedStroke(
                color = Color.WHITE,
                radius = 22f,
                strokeColor = Color.rgb(232, 235, 240),
            )
            elevation = 0f
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

    private fun humanLeadAttributes(lead: JSONObject): List<Pair<String, String>> {
        val result = mutableListOf<Pair<String, String>>()
        val details = lead.optJSONArray("attribute_details")
        if (details != null) {
            for (i in 0 until details.length()) {
                val row = details.optJSONObject(i) ?: continue
                val label = row.optString("name").trim()
                val rawValue = row.optString("value").trim()
                val value = formatLeadAttributeValue(row.optString("field_type"), rawValue)
                if (label.isNotBlank() && value.isNotBlank()) {
                    result.add(label to value)
                }
            }
            return result
        }

        // Backward-compatible fallback for an older backend. Keep it conservative:
        // never show nested JSON or technical/system-looking keys.
        val attributes = lead.optJSONObject("attributes") ?: return result
        val keys = attributes.keys()
        while (keys.hasNext()) {
            val key = keys.next()
            if (!isHumanLeadAttributeKey(key)) continue
            val raw = attributes.opt(key)
            if (raw is JSONObject || raw is org.json.JSONArray) continue
            val value = raw?.toString()?.trim().orEmpty()
            if (value.isBlank() || value.equals("null", ignoreCase = true)) continue
            if (looksLikeJson(value)) continue
            result.add(humanizeAttributeKey(key) to value)
        }
        return result
    }

    private fun formatLeadAttributeValue(fieldType: String, rawValue: String): String {
        if (rawValue.isBlank() || looksLikeJson(rawValue)) return ""
        return when (fieldType) {
            "date" -> try {
                val parsed = SimpleDateFormat("yyyy-MM-dd", Locale.US).parse(rawValue)
                if (parsed == null) rawValue else SimpleDateFormat("d MMM yyyy", Locale.getDefault()).format(parsed)
            } catch (_: Exception) {
                rawValue
            }
            "datetime" -> formatDate(rawValue)
            else -> rawValue
        }
    }

    private fun looksLikeJson(value: String): Boolean {
        val trimmed = value.trim()
        if (!(trimmed.startsWith("{") || trimmed.startsWith("["))) return false
        return runCatching {
            if (trimmed.startsWith("{")) JSONObject(trimmed) else org.json.JSONArray(trimmed)
            true
        }.getOrDefault(false)
    }

    private fun isHumanLeadAttributeKey(key: String): Boolean {
        val normalized = key.trim().lowercase(Locale.US)
        if (normalized.isBlank() || normalized.startsWith("_") || normalized.endsWith("_id")) return false
        if (normalized in setOf(
                "id", "uuid", "created_at", "updated_at", "organization_id",
                "pipeline_id", "stage_id", "owner_id", "user_id", "booked_at"
            )) return false
        return listOf("metadata", "payload", "trace", "internal", "raw_json", "system_")
            .none { normalized.contains(it) }
    }

    private fun humanizeAttributeKey(key: String): String =
        key.replace('_', ' ')
            .replace('-', ' ')
            .trim()
            .split(Regex("\\s+"))
            .filter { it.isNotBlank() }
            .joinToString(" ") { word -> word.replaceFirstChar { it.uppercase() } }

    private fun priorityButton(active: Boolean): LinearLayout =
        LinearLayout(this).apply {
            gravity = Gravity.CENTER
            isClickable = true
            isFocusable = true
            contentDescription = if (active) "Remove from priority leads" else "Add to priority leads"
            val fill = if (active) Color.rgb(255, 247, 224) else Color.rgb(247, 248, 250)
            val ripple = if (active) Color.rgb(244, 218, 155) else Color.rgb(222, 226, 232)
            background = RippleDrawable(
                ColorStateList.valueOf(ripple),
                roundedStroke(
                    color = fill,
                    radius = 15f,
                    strokeColor = if (active) Color.rgb(239, 205, 121) else Color.rgb(226, 230, 235),
                ),
                null,
            )
            addView(TextView(this@MainActivity).apply {
                text = "★"
                textSize = 23f
                gravity = Gravity.CENTER
                setTextColor(
                    if (active) Color.rgb(224, 154, 25)
                    else Color.rgb(150, 157, 168)
                )
                setPadding(0, 0, dp(7), dp(1))
            })
            addView(TextView(this@MainActivity).apply {
                text = "Priority"
                textSize = 13f
                setTypeface(typeface, Typeface.BOLD)
                setTextColor(if (active) Color.rgb(116, 78, 11) else ink)
            })
        }

    private fun detailRow(label: String, value: String): LinearLayout =
        LinearLayout(this).apply {
            orientation = LinearLayout.VERTICAL
            setPadding(0, dp(12), 0, dp(12))
            addView(body(label).apply {
                textSize = 12f
                setTypeface(typeface, Typeface.BOLD)
                setPadding(0, 0, 0, 0)
            })
            addView(body(value).apply {
                textSize = 15f
                setTextColor(ink)
                setPadding(0, dp(4), 0, 0)
            })
        }

    private fun statusLine(label: String, ok: Boolean): TextView =
        TextView(this).apply {
            text = (if (ok) "✓  " else "○  ") + label
            textSize = 13f
            setTextColor(if (ok) Color.rgb(38, 132, 53) else Color.rgb(125, 125, 130))
            setPadding(0, dp(7), 0, dp(7))
        }

    private fun workspaceStatusCard(): LinearLayout = card().apply {
        val ready = essentialPermissionsGranted()
        val top = LinearLayout(this@MainActivity).apply { gravity = Gravity.CENTER_VERTICAL }
        top.addView(iconBadge(
            if (ready) R.drawable.ic_phone_outline else R.drawable.ic_settings_outline,
            if (ready) Color.rgb(35, 139, 100) else blue,
            if (ready) Color.rgb(235, 248, 242) else Color.rgb(236, 244, 255),
        ))
        top.addView(LinearLayout(this@MainActivity).apply {
            orientation = LinearLayout.VERTICAL
            addView(sectionTitle(if (ready) "Workspace ready" else "Finish mobile setup").apply {
                textSize = 18f
                setPadding(0, 0, 0, 0)
            })
            addView(body(auth.email.ifBlank { "Signed in to SHVYA" }).apply {
                textSize = 12f
                setPadding(0, dp(3), 0, 0)
                maxLines = 1
                ellipsize = android.text.TextUtils.TruncateAt.END
            })
        }, LinearLayout.LayoutParams(0, -2, 1f).apply { leftMargin = dp(13) })
        top.addView(TextView(this@MainActivity).apply {
            text = if (ready) "Connected" else "Setup"
            textSize = 11f
            setTypeface(typeface, Typeface.BOLD)
            setTextColor(if (ready) Color.rgb(35, 139, 100) else blue)
            gravity = Gravity.CENTER
            setPadding(dp(10), 0, dp(10), 0)
            minimumHeight = dp(30)
            background = rounded(
                if (ready) Color.rgb(235, 248, 242) else Color.rgb(236, 244, 255),
                15f,
            )
        })
        addView(top)
        addView(space(16))
        addView(divider())
        addView(space(13))
        addView(body(
            if (ready) "Calls sync securely in the background. Open Reminders anytime to stay on top of follow-ups."
            else "Allow phone and call-log access once. SHVYA will then keep your call activity organized automatically."
        ).apply { setPadding(0, 0, 0, 0) })
    }

    private fun loginTextField(
        hintText: String,
        inputTypeValue: Int,
        autofillHint: String? = null,
    ): Pair<LinearLayout, EditText> {
        val input = EditText(this).apply {
            hint = hintText
            textSize = 15f
            setTextColor(ink)
            setHintTextColor(Color.rgb(142, 150, 162))
            setPadding(dp(16), 0, dp(16), 0)
            inputType = inputTypeValue
            isSingleLine = true
            background = null
            if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.O && autofillHint != null) {
                setAutofillHints(autofillHint)
            }
        }
        val shell = LinearLayout(this).apply {
            gravity = Gravity.CENTER_VERTICAL
            minimumHeight = dp(56)
            background = roundedStroke(
                color = Color.rgb(248, 249, 251),
                radius = 16f,
                strokeColor = Color.rgb(224, 228, 234),
            )
            addView(input, LinearLayout.LayoutParams(0, dp(56), 1f))
        }
        return shell to input
    }

    private fun passwordField(): Pair<LinearLayout, EditText> {
        val input = EditText(this).apply {
            hint = "Enter your password"
            textSize = 15f
            setTextColor(ink)
            setHintTextColor(Color.rgb(142, 150, 162))
            setPadding(dp(16), 0, dp(6), 0)
            inputType = InputType.TYPE_CLASS_TEXT or InputType.TYPE_TEXT_VARIATION_PASSWORD
            transformationMethod = PasswordTransformationMethod.getInstance()
            isSingleLine = true
            background = null
            if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.O) {
                setAutofillHints(View.AUTOFILL_HINT_PASSWORD)
            }
        }
        val eye = ImageView(this).apply {
            setImageResource(R.drawable.ic_eye_outline)
            imageTintList = ColorStateList.valueOf(muted)
            contentDescription = "Show password"
            setPadding(dp(14), dp(14), dp(14), dp(14))
            isClickable = true
            isFocusable = true
            var visible = false
            setOnClickListener {
                visible = !visible
                input.transformationMethod = if (visible) {
                    HideReturnsTransformationMethod.getInstance()
                } else {
                    PasswordTransformationMethod.getInstance()
                }
                setImageResource(if (visible) R.drawable.ic_eye_off_outline else R.drawable.ic_eye_outline)
                contentDescription = if (visible) "Hide password" else "Show password"
                input.setSelection(input.text.length)
            }
        }
        val shell = LinearLayout(this).apply {
            gravity = Gravity.CENTER_VERTICAL
            minimumHeight = dp(56)
            background = roundedStroke(
                color = Color.rgb(248, 249, 251),
                radius = 16f,
                strokeColor = Color.rgb(224, 228, 234),
            )
            addView(input, LinearLayout.LayoutParams(0, dp(56), 1f))
            addView(eye, LinearLayout.LayoutParams(dp(52), dp(56)))
        }
        return shell to input
    }

    private fun statusDot(color: Int): View = View(this).apply {
        background = rounded(color, 4f)
        layoutParams = LinearLayout.LayoutParams(dp(8), dp(8))
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

    private fun roundedStroke(
        color: Int,
        radius: Float,
        strokeColor: Int,
        strokeWidth: Int = 1,
    ): GradientDrawable =
        GradientDrawable().apply {
            setColor(color)
            cornerRadius = dp(radius.toInt()).toFloat()
            setStroke(dp(strokeWidth), strokeColor)
        }

    private fun space(height: Int): View =
        View(this).apply {
            layoutParams = LinearLayout.LayoutParams(1, dp(height))
        }

    private fun dp(value: Int): Int =
        (value * resources.displayMetrics.density).toInt()
}
