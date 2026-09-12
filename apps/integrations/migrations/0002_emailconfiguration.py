import uuid

from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):

    dependencies = [
        ("integrations", "0001_webhookconfiguration_webhookdelivery"),
    ]

    operations = [
        migrations.CreateModel(
            name="EmailConfiguration",
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
                    "provider",
                    models.CharField(
                        choices=[
                            ("gmail", "Gmail / Google Workspace"),
                            ("microsoft", "Microsoft 365 / Outlook"),
                            ("zoho", "Zoho Mail"),
                            ("custom", "Custom SMTP"),
                        ],
                        default="custom",
                        max_length=20,
                    ),
                ),
                ("email_address", models.EmailField(max_length=254)),
                ("sender_name", models.CharField(blank=True, max_length=120)),
                (
                    "reply_to_email",
                    models.EmailField(blank=True, max_length=254),
                ),
                ("smtp_host", models.CharField(max_length=255)),
                ("smtp_port", models.PositiveIntegerField(default=587)),
                (
                    "smtp_security",
                    models.CharField(
                        choices=[
                            ("starttls", "STARTTLS"),
                            ("ssl", "SSL/TLS"),
                            ("none", "None"),
                        ],
                        default="starttls",
                        max_length=16,
                    ),
                ),
                ("smtp_username", models.CharField(max_length=254)),
                ("encrypted_password", models.TextField(blank=True)),
                ("is_enabled", models.BooleanField(default=False)),
                (
                    "last_test_status",
                    models.CharField(
                        choices=[
                            ("not_tested", "Not tested"),
                            ("success", "Connected"),
                            ("failed", "Connection failed"),
                        ],
                        default="not_tested",
                        max_length=16,
                    ),
                ),
                ("last_tested_at", models.DateTimeField(blank=True, null=True)),
                ("last_error", models.TextField(blank=True)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                (
                    "organization",
                    models.OneToOneField(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="email_configuration",
                        to="organizations.organization",
                    ),
                ),
            ],
            options={
                "verbose_name": "Email Configuration",
                "verbose_name_plural": "Email Configurations",
                "ordering": ["organization__name"],
            },
        ),
    ]
