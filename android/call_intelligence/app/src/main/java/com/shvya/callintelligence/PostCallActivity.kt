package com.shvya.callintelligence

import android.graphics.Color
import android.graphics.Typeface
import android.graphics.drawable.GradientDrawable
import android.os.Bundle
import android.view.Gravity
import android.widget.Button
import android.widget.EditText
import android.widget.LinearLayout
import android.widget.ScrollView
import android.widget.TextView
import androidx.appcompat.app.AppCompatActivity
import com.shvya.callintelligence.calls.CallCaptureRepository
import com.shvya.callintelligence.data.AppDatabase
import com.shvya.callintelligence.workers.WorkerScheduler
import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.Job
import kotlinx.coroutines.cancel
import kotlinx.coroutines.launch
import kotlinx.coroutines.withContext
import java.text.SimpleDateFormat
import java.util.Calendar
import java.util.Date
import java.util.Locale

class PostCallActivity : AppCompatActivity() {
    companion object {
        const val EXTRA_SOURCE_CALL_ID = "source_call_id"
    }

    private val scope = CoroutineScope(Job() + Dispatchers.Main)
    private var followUpAtMillis: Long? = null
    private lateinit var followUpLabel: TextView

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        val sourceCallId = intent.getStringExtra(EXTRA_SOURCE_CALL_ID).orEmpty()
        if (sourceCallId.isBlank()) {
            finish()
            return
        }

        scope.launch {
            val call = withContext(Dispatchers.IO) {
                AppDatabase.get(this@PostCallActivity)
                    .callDao()
                    .bySourceCallId(sourceCallId)
            }
            if (call == null) {
                finish()
                return@launch
            }

            followUpAtMillis = call.followUpAtMillis
            render(
                sourceCallId = sourceCallId,
                title = call.contactName.ifBlank { call.phoneNumber },
                phone = call.phoneNumber,
                initialNotes = call.notes,
                initialDisposition = call.disposition,
            )
        }
    }

    override fun onDestroy() {
        scope.cancel()
        super.onDestroy()
    }

    private fun render(
        sourceCallId: String,
        title: String,
        phone: String,
        initialNotes: String,
        initialDisposition: String,
    ) {
        val scroll = ScrollView(this).apply {
            setBackgroundColor(Color.rgb(245, 245, 247))
        }
        val root = LinearLayout(this).apply {
            orientation = LinearLayout.VERTICAL
            setPadding(dp(24), dp(40), dp(24), dp(40))
        }
        scroll.addView(root)
        setContentView(scroll)

        root.addView(TextView(this).apply {
            text = "POST-CALL"
            textSize = 12f
            letterSpacing = .08f
            typeface = Typeface.DEFAULT_BOLD
            setTextColor(Color.rgb(0, 113, 227))
        })
        root.addView(TextView(this).apply {
            text = title
            textSize = 38f
            typeface = Typeface.DEFAULT_BOLD
            setTextColor(Color.rgb(29, 29, 31))
            setPadding(0, dp(8), 0, dp(4))
        })
        root.addView(TextView(this).apply {
            text = phone
            textSize = 15f
            setTextColor(Color.rgb(110, 110, 115))
            setPadding(0, 0, 0, dp(24))
        })

        val notes = field(
            root,
            "What happened on the call?",
            initialNotes,
            minLines = 4,
        )
        val disposition = field(
            root,
            "Outcome (for example: demo requested, no answer, interested)",
            initialDisposition,
            minLines = 1,
        )

        root.addView(TextView(this).apply {
            text = "Follow-up"
            textSize = 20f
            typeface = Typeface.DEFAULT_BOLD
            setTextColor(Color.rgb(29, 29, 31))
            setPadding(0, dp(16), 0, dp(10))
        })

        followUpLabel = TextView(this).apply {
            textSize = 14f
            setTextColor(Color.rgb(110, 110, 115))
            setPadding(0, 0, 0, dp(10))
        }
        root.addView(followUpLabel)
        syncFollowUpLabel()

        val quick = LinearLayout(this).apply {
            orientation = LinearLayout.HORIZONTAL
            gravity = Gravity.CENTER_VERTICAL
        }
        quick.addView(quickButton("+30 min") {
            followUpAtMillis = System.currentTimeMillis() + 30 * 60 * 1000L
            syncFollowUpLabel()
        }, LinearLayout.LayoutParams(0, dp(44), 1f).apply { marginEnd = dp(8) })
        quick.addView(quickButton("Tomorrow 11") {
            followUpAtMillis = Calendar.getInstance().apply {
                add(Calendar.DAY_OF_YEAR, 1)
                set(Calendar.HOUR_OF_DAY, 11)
                set(Calendar.MINUTE, 0)
                set(Calendar.SECOND, 0)
                set(Calendar.MILLISECOND, 0)
            }.timeInMillis
            syncFollowUpLabel()
        }, LinearLayout.LayoutParams(0, dp(44), 1f).apply { marginEnd = dp(8) })
        quick.addView(quickButton("None") {
            followUpAtMillis = null
            syncFollowUpLabel()
        }, LinearLayout.LayoutParams(0, dp(44), 1f))
        root.addView(quick)

        val save = Button(this).apply {
            text = "Save to SHVYA"
            isAllCaps = false
            textSize = 16f
            typeface = Typeface.DEFAULT_BOLD
            setTextColor(Color.WHITE)
            background = rounded(Color.rgb(0, 113, 227), 28f)
            setOnClickListener {
                isEnabled = false
                text = "Saving…"
                scope.launch {
                    withContext(Dispatchers.IO) {
                        CallCaptureRepository(this@PostCallActivity).enrich(
                            sourceCallId = sourceCallId,
                            notes = notes.text.toString(),
                            disposition = disposition.text.toString(),
                            followUpAtMillis = followUpAtMillis,
                        )
                    }
                    WorkerScheduler.enqueueSync(this@PostCallActivity)
                    finish()
                }
            }
        }
        root.addView(
            save,
            LinearLayout.LayoutParams(
                LinearLayout.LayoutParams.MATCH_PARENT,
                dp(54),
            ).apply { topMargin = dp(24) },
        )
    }

    private fun field(
        root: LinearLayout,
        hint: String,
        value: String,
        minLines: Int,
    ): EditText {
        val field = EditText(this).apply {
            this.hint = hint
            setText(value)
            setTextColor(Color.rgb(29, 29, 31))
            setHintTextColor(Color.rgb(134, 134, 139))
            textSize = 15f
            setPadding(dp(16), dp(14), dp(16), dp(14))
            setMinLines(minLines)
            gravity = Gravity.TOP
            background = rounded(Color.WHITE, 16f)
        }
        root.addView(
            field,
            LinearLayout.LayoutParams(
                LinearLayout.LayoutParams.MATCH_PARENT,
                LinearLayout.LayoutParams.WRAP_CONTENT,
            ).apply { bottomMargin = dp(12) },
        )
        return field
    }

    private fun quickButton(label: String, action: () -> Unit): Button =
        Button(this).apply {
            text = label
            isAllCaps = false
            textSize = 12f
            setTextColor(Color.rgb(0, 113, 227))
            background = rounded(Color.WHITE, 22f)
            setOnClickListener { action() }
        }

    private fun syncFollowUpLabel() {
        followUpLabel.text = followUpAtMillis?.let {
            "Reminder: " + SimpleDateFormat(
                "EEE, d MMM · h:mm a",
                Locale.getDefault(),
            ).format(Date(it))
        } ?: "No reminder selected"
    }

    private fun rounded(fill: Int, radiusDp: Float): GradientDrawable =
        GradientDrawable().apply {
            cornerRadius = dp(radiusDp.toInt()).toFloat()
            setColor(fill)
        }

    private fun dp(value: Int): Int =
        (value * resources.displayMetrics.density).toInt()
}
