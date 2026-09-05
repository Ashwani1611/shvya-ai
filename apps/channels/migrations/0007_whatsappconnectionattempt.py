# Generated for WhatsApp connection-attempt audit tracking.

import django.db.models.deletion
import uuid
from django.conf import settings
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("channels", "0006_merge_20260905_1219"),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.CreateModel(
            name="WhatsAppConnectionAttempt",
            fields=[
                (
                    "id",
                    models.UUIDField(
                        default=uuid.uuid4,
                        editable=False,
                        primary_key=True,
                        serialize=False,
                    ),
                ),
                (
                    "method",
                    models.CharField(
                        choices=[
                            ("embedded", "Meta Embedded Signup"),
                            ("manual", "Manual Access Token"),
                        ],
                        max_length=16,
                    ),
                ),
                (
                    "status",
                    models.CharField(
                        choices=[
                            ("started", "Started"),
                            ("meta_finished", "Meta Finished"),
                            ("code_received", "OAuth Code Received"),
                            ("callback_received", "Backend Callback Received"),
                            ("token_exchanged", "Token Exchanged"),
                            ("phone_verified", "Phone Verified"),
                            ("connected", "Connected"),
                            ("failed", "Failed"),
                            ("cancelled", "Cancelled"),
                        ],
                        default="started",
                        max_length=24,
                    ),
                ),
                ("stage", models.CharField(blank=True, max_length=64)),
                ("waba_id", models.CharField(blank=True, max_length=64)),
                ("phone_number_id", models.CharField(blank=True, max_length=64)),
                ("display_phone_number", models.CharField(blank=True, max_length=32)),
                ("business_name", models.CharField(blank=True, max_length=150)),
                ("code_received", models.BooleanField(default=False)),
                ("token_received", models.BooleanField(default=False)),
                ("webhook_subscribed", models.BooleanField(blank=True, null=True)),
                ("meta_error_code", models.CharField(blank=True, max_length=64)),
                ("error_message", models.TextField(blank=True)),
                ("warning_message", models.TextField(blank=True)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                ("completed_at", models.DateTimeField(blank=True, null=True)),
                (
                    "account",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.SET_NULL,
                        related_name="connection_attempts",
                        to="channels.whatsappaccount",
                    ),
                ),
                (
                    "created_by",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.SET_NULL,
                        related_name="whatsapp_connection_attempts",
                        to=settings.AUTH_USER_MODEL,
                    ),
                ),
                (
                    "organization",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="whatsapp_connection_attempts",
                        to="organizations.organization",
                    ),
                ),
            ],
            options={
                "ordering": ["-created_at"],
            },
        ),
        migrations.AddIndex(
            model_name="whatsappconnectionattempt",
            index=models.Index(
                fields=["organization", "created_at"],
                name="wa_attempt_org_created_idx",
            ),
        ),
        migrations.AddIndex(
            model_name="whatsappconnectionattempt",
            index=models.Index(
                fields=["organization", "status"],
                name="wa_attempt_org_status_idx",
            ),
        ),
    ]
