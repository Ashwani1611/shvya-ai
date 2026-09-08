import uuid

import django.db.models.deletion
from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("ai_engagement", "0013_document_share_instruction"),
        ("organizations", "0004_organizationpayment"),
    ]

    operations = [
        migrations.CreateModel(
            name="AICreditWallet",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("balance", models.BigIntegerField(default=0)),
                ("reserved_credits", models.PositiveBigIntegerField(default=0)),
                ("lifetime_credits_added", models.PositiveBigIntegerField(default=0)),
                ("lifetime_credits_used", models.PositiveBigIntegerField(default=0)),
                ("is_blocked", models.BooleanField(default=False)),
                ("low_credit_threshold", models.PositiveBigIntegerField(default=100)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                (
                    "organization",
                    models.OneToOneField(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="ai_credit_wallet",
                        to="organizations.organization",
                    ),
                ),
            ],
            options={
                "verbose_name": "AI Credit Wallet",
                "verbose_name_plural": "AI Credit Wallets",
                "ordering": ["organization__name"],
            },
        ),
        migrations.CreateModel(
            name="AICreditReservation",
            fields=[
                ("id", models.UUIDField(default=uuid.uuid4, editable=False, primary_key=True, serialize=False)),
                ("feature", models.CharField(default="other", max_length=64)),
                ("model", models.CharField(blank=True, max_length=150)),
                ("reference_id", models.CharField(blank=True, max_length=150)),
                ("reserved_credits", models.PositiveBigIntegerField()),
                ("estimated_input_tokens", models.PositiveBigIntegerField(default=0)),
                ("estimated_output_tokens", models.PositiveBigIntegerField(default=0)),
                ("actual_credits", models.PositiveBigIntegerField(default=0)),
                ("actual_input_tokens", models.PositiveBigIntegerField(default=0)),
                ("actual_output_tokens", models.PositiveBigIntegerField(default=0)),
                (
                    "status",
                    models.CharField(
                        choices=[("active", "Active"), ("settled", "Settled"), ("released", "Released")],
                        default="active",
                        max_length=20,
                    ),
                ),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("settled_at", models.DateTimeField(blank=True, null=True)),
                (
                    "organization",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="ai_credit_reservations",
                        to="organizations.organization",
                    ),
                ),
                (
                    "wallet",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="reservations",
                        to="ai_engagement.aicreditwallet",
                    ),
                ),
            ],
            options={"ordering": ["-created_at"]},
        ),
        migrations.CreateModel(
            name="AICreditTransaction",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                (
                    "transaction_type",
                    models.CharField(
                        choices=[
                            ("manual_credit", "Manual credit"),
                            ("manual_debit", "Manual debit"),
                            ("ai_usage", "AI usage"),
                        ],
                        max_length=30,
                    ),
                ),
                ("amount", models.BigIntegerField(help_text="Signed credits. Positive adds balance; negative consumes it.")),
                ("balance_after", models.BigIntegerField()),
                ("feature", models.CharField(blank=True, max_length=64)),
                ("model", models.CharField(blank=True, max_length=150)),
                ("input_tokens", models.PositiveBigIntegerField(default=0)),
                ("output_tokens", models.PositiveBigIntegerField(default=0)),
                ("reservation_id", models.UUIDField(blank=True, null=True)),
                ("reference_id", models.CharField(blank=True, max_length=150)),
                ("description", models.CharField(blank=True, max_length=500)),
                ("actor_id", models.CharField(blank=True, max_length=64)),
                ("actor_label", models.CharField(blank=True, max_length=255)),
                ("metadata", models.JSONField(blank=True, default=dict)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                (
                    "organization",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="ai_credit_transactions",
                        to="organizations.organization",
                    ),
                ),
                (
                    "wallet",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="transactions",
                        to="ai_engagement.aicreditwallet",
                    ),
                ),
            ],
            options={"ordering": ["-created_at", "-id"]},
        ),
        migrations.AddIndex(
            model_name="aicreditreservation",
            index=models.Index(fields=["organization", "status", "created_at"], name="ai_engageme_organiz_4489d8_idx"),
        ),
        migrations.AddIndex(
            model_name="aicredittransaction",
            index=models.Index(fields=["organization", "created_at"], name="ai_engageme_organiz_08a8ae_idx"),
        ),
        migrations.AddIndex(
            model_name="aicredittransaction",
            index=models.Index(fields=["organization", "transaction_type", "created_at"], name="ai_engageme_organiz_8cb254_idx"),
        ),
    ]
