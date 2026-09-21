# Generated for SHVYA Call Intelligence.
import django.db.models.deletion
import uuid
from django.conf import settings
from django.db import migrations, models
from django.db.models import Q


class Migration(migrations.Migration):

    initial = True

    dependencies = [
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
        ("organizations", "0005_apikey_can_read_diagnostics"),
        ("crm", "0028_alter_lead_lead_source"),
    ]

    operations = [
        migrations.CreateModel(
            name="CallIntelligenceSettings",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("is_enabled", models.BooleanField(default=True)),
                ("auto_create_answered", models.BooleanField(default=True)),
                ("auto_create_missed", models.BooleanField(default=True)),
                ("auto_create_outbound", models.BooleanField(default=True)),
                ("default_country_code", models.CharField(default="+91", max_length=8)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                ("default_pipeline", models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name="+", to="crm.pipeline")),
                ("default_stage", models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name="+", to="crm.stage")),
                ("organization", models.OneToOneField(on_delete=django.db.models.deletion.CASCADE, related_name="call_intelligence_settings", to="organizations.organization")),
            ],
        ),
        migrations.CreateModel(
            name="CallDevice",
            fields=[
                ("id", models.UUIDField(default=uuid.uuid4, editable=False, primary_key=True, serialize=False)),
                ("device_uuid", models.UUIDField()),
                ("device_name", models.CharField(blank=True, max_length=150)),
                ("manufacturer", models.CharField(blank=True, max_length=100)),
                ("model", models.CharField(blank=True, max_length=100)),
                ("android_version", models.CharField(blank=True, max_length=40)),
                ("app_version", models.CharField(blank=True, max_length=40)),
                ("permissions", models.JSONField(blank=True, default=dict)),
                ("battery_optimization_ignored", models.BooleanField(default=False)),
                ("is_active", models.BooleanField(default=True)),
                ("registered_at", models.DateTimeField(auto_now_add=True)),
                ("last_seen_at", models.DateTimeField(auto_now=True)),
                ("organization", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name="call_intelligence_devices", to="organizations.organization")),
                ("user", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name="call_intelligence_devices", to=settings.AUTH_USER_MODEL)),
            ],
            options={"ordering": ["-last_seen_at"]},
        ),
        migrations.CreateModel(
            name="CallRecord",
            fields=[
                ("id", models.UUIDField(default=uuid.uuid4, editable=False, primary_key=True, serialize=False)),
                ("source", models.CharField(choices=[("android_sim", "Android SIM"), ("manual", "Manual"), ("provider", "Provider")], default="android_sim", max_length=30)),
                ("source_call_id", models.CharField(blank=True, max_length=160)),
                ("direction", models.CharField(choices=[("inbound", "Incoming"), ("outbound", "Outgoing"), ("unknown", "Unknown")], default="unknown", max_length=20)),
                ("status", models.CharField(choices=[("initiated", "Initiated"), ("ringing", "Ringing"), ("answered", "Answered"), ("completed", "Completed"), ("missed", "Missed"), ("rejected", "Rejected"), ("busy", "Busy"), ("no_answer", "No Answer"), ("failed", "Failed"), ("unknown", "Unknown")], default="unknown", max_length=20)),
                ("lead_match_status", models.CharField(choices=[("matched", "Matched"), ("auto_created", "Auto-created"), ("unmatched", "Unmatched"), ("restricted", "Matched outside user access")], default="unmatched", max_length=20)),
                ("phone_number", models.CharField(max_length=32)),
                ("raw_phone_number", models.CharField(blank=True, max_length=64)),
                ("contact_name", models.CharField(blank=True, max_length=180)),
                ("sim_slot", models.CharField(blank=True, max_length=30)),
                ("started_at", models.DateTimeField(blank=True, null=True)),
                ("ringing_at", models.DateTimeField(blank=True, null=True)),
                ("answered_at", models.DateTimeField(blank=True, null=True)),
                ("ended_at", models.DateTimeField(blank=True, null=True)),
                ("called_at", models.DateTimeField()),
                ("ring_duration_seconds", models.PositiveIntegerField(default=0)),
                ("duration_seconds", models.PositiveIntegerField(default=0)),
                ("notes", models.TextField(blank=True)),
                ("disposition", models.CharField(blank=True, max_length=80)),
                ("follow_up_required", models.BooleanField(default=False)),
                ("follow_up_at", models.DateTimeField(blank=True, null=True)),
                ("metadata", models.JSONField(blank=True, default=dict)),
                ("first_received_at", models.DateTimeField(auto_now_add=True)),
                ("last_received_at", models.DateTimeField(auto_now=True)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                ("crm_call", models.OneToOneField(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name="intelligence_record", to="crm.leadcall")),
                ("device", models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name="calls", to="calls.calldevice")),
                ("lead", models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name="intelligence_calls", to="crm.lead")),
                ("organization", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name="call_intelligence_calls", to="organizations.organization")),
                ("pipeline", models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name="+", to="crm.pipeline")),
                ("stage", models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name="+", to="crm.stage")),
                ("user", models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name="call_intelligence_calls", to=settings.AUTH_USER_MODEL)),
            ],
            options={"ordering": ["-called_at", "-created_at"]},
        ),
        migrations.CreateModel(
            name="CallEvent",
            fields=[
                ("id", models.UUIDField(default=uuid.uuid4, editable=False, primary_key=True, serialize=False)),
                ("event_uuid", models.UUIDField(unique=True)),
                ("event_type", models.CharField(choices=[("detected", "Detected"), ("ringing", "Ringing"), ("offhook", "Off hook"), ("answered", "Answered"), ("idle", "Idle"), ("completed", "Completed"), ("missed", "Missed"), ("rejected", "Rejected"), ("reconciled", "Call log reconciled")], max_length=30)),
                ("occurred_at", models.DateTimeField()),
                ("payload", models.JSONField(blank=True, default=dict)),
                ("received_at", models.DateTimeField(auto_now_add=True)),
                ("call", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name="events", to="calls.callrecord")),
                ("device", models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name="events", to="calls.calldevice")),
            ],
            options={"ordering": ["occurred_at", "received_at"]},
        ),
        migrations.CreateModel(
            name="CallIntelligence",
            fields=[
                ("id", models.UUIDField(default=uuid.uuid4, editable=False, primary_key=True, serialize=False)),
                ("analysis_status", models.CharField(choices=[("not_available", "Not available"), ("pending", "Pending"), ("processing", "Processing"), ("completed", "Completed"), ("failed", "Failed")], default="not_available", max_length=20)),
                ("recording_url", models.URLField(blank=True, max_length=1000)),
                ("transcript", models.TextField(blank=True)),
                ("summary", models.TextField(blank=True)),
                ("intent", models.CharField(choices=[("unknown", "Unknown"), ("low", "Low"), ("medium", "Medium"), ("high", "High")], default="unknown", max_length=20)),
                ("sentiment", models.CharField(blank=True, max_length=40)),
                ("outcome", models.CharField(blank=True, max_length=100)),
                ("objections", models.JSONField(blank=True, default=list)),
                ("buying_signals", models.JSONField(blank=True, default=list)),
                ("competitors", models.JSONField(blank=True, default=list)),
                ("extracted_attributes", models.JSONField(blank=True, default=dict)),
                ("next_action", models.CharField(blank=True, max_length=255)),
                ("follow_up_at", models.DateTimeField(blank=True, null=True)),
                ("ai_score", models.DecimalField(blank=True, decimal_places=2, max_digits=4, null=True)),
                ("talk_ratio", models.JSONField(blank=True, default=dict)),
                ("analysis_payload", models.JSONField(blank=True, default=dict)),
                ("analyzed_at", models.DateTimeField(blank=True, null=True)),
                ("analysis_error", models.TextField(blank=True)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                ("call", models.OneToOneField(on_delete=django.db.models.deletion.CASCADE, related_name="intelligence", to="calls.callrecord")),
            ],
        ),
        migrations.AddConstraint(
            model_name="calldevice",
            constraint=models.UniqueConstraint(fields=("organization", "device_uuid"), name="uniq_call_device_org_uuid"),
        ),
        migrations.AddIndex(
            model_name="calldevice",
            index=models.Index(fields=["organization", "user", "-last_seen_at"], name="calls_device_org_user_seen_idx"),
        ),
        migrations.AddConstraint(
            model_name="callrecord",
            constraint=models.UniqueConstraint(condition=~Q(source_call_id=""), fields=("organization", "source", "source_call_id"), name="uniq_call_org_source_id"),
        ),
        migrations.AddIndex(
            model_name="callrecord",
            index=models.Index(fields=["organization", "-called_at"], name="calls_org_called_idx"),
        ),
        migrations.AddIndex(
            model_name="callrecord",
            index=models.Index(fields=["organization", "phone_number", "-called_at"], name="calls_org_phone_called_idx"),
        ),
        migrations.AddIndex(
            model_name="callrecord",
            index=models.Index(fields=["user", "-called_at"], name="calls_user_called_idx"),
        ),
        migrations.AddIndex(
            model_name="callrecord",
            index=models.Index(fields=["lead", "-called_at"], name="calls_lead_called_idx"),
        ),
        migrations.AddIndex(
            model_name="callrecord",
            index=models.Index(fields=["organization", "status", "-called_at"], name="calls_org_status_called_idx"),
        ),
        migrations.AddIndex(
            model_name="callevent",
            index=models.Index(fields=["call", "occurred_at"], name="calls_event_call_time_idx"),
        ),
    ]
