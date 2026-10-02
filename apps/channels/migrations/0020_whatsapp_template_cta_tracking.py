import uuid

import django.db.models.deletion
from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [("channels", "0019_template_delivery_media")]

    operations = [
        migrations.CreateModel(
            name="WhatsAppTemplateTrackedLink",
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
                    "token",
                    models.UUIDField(default=uuid.uuid4, editable=False, unique=True),
                ),
                ("meta_template_id", models.CharField(blank=True, db_index=True, max_length=128)),
                ("template_name", models.CharField(blank=True, max_length=150)),
                ("button_path", models.CharField(max_length=64)),
                ("button_index", models.PositiveSmallIntegerField(default=0)),
                ("card_index", models.PositiveSmallIntegerField(blank=True, null=True)),
                (
                    "action_type",
                    models.CharField(
                        choices=[
                            ("website", "Website"),
                            ("call", "Call"),
                            ("copy_code", "Copy code"),
                        ],
                        max_length=20,
                    ),
                ),
                ("button_text", models.CharField(blank=True, max_length=80)),
                ("destination_url", models.URLField(blank=True, max_length=2048)),
                ("phone_number", models.CharField(blank=True, max_length=32)),
                ("coupon_code", models.CharField(blank=True, max_length=128)),
                ("is_active", models.BooleanField(db_index=True, default=False)),
                ("sent_at", models.DateTimeField(blank=True, db_index=True, null=True)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                (
                    "account",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="template_tracked_links",
                        to="channels.whatsappaccount",
                    ),
                ),
                (
                    "lead",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.SET_NULL,
                        related_name="whatsapp_template_tracked_links",
                        to="crm.lead",
                    ),
                ),
                (
                    "message",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="template_tracked_links",
                        to="channels.whatsappmessage",
                    ),
                ),
                (
                    "organization",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="whatsapp_template_tracked_links",
                        to="organizations.organization",
                    ),
                ),
                (
                    "template",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.SET_NULL,
                        related_name="tracked_links",
                        to="channels.whatsapptemplate",
                    ),
                ),
            ],
        ),
        migrations.CreateModel(
            name="WhatsAppTemplateTrackedClick",
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
                    "event_type",
                    models.CharField(
                        choices=[("click", "CTA click"), ("action", "Action completed")],
                        default="click",
                        max_length=12,
                    ),
                ),
                ("fingerprint", models.CharField(blank=True, db_index=True, max_length=64)),
                ("user_agent", models.CharField(blank=True, max_length=500)),
                ("created_at", models.DateTimeField(auto_now_add=True, db_index=True)),
                (
                    "link",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="events",
                        to="channels.whatsapptemplatetrackedlink",
                    ),
                ),
            ],
            options={"ordering": ["-created_at"]},
        ),
        migrations.AddConstraint(
            model_name="whatsapptemplatetrackedlink",
            constraint=models.UniqueConstraint(
                fields=("message", "button_path"),
                name="wa_cta_link_message_path_uniq",
            ),
        ),
        migrations.AddIndex(
            model_name="whatsapptemplatetrackedlink",
            index=models.Index(
                fields=["account", "sent_at"],
                name="wa_cta_link_acct_sent_idx",
            ),
        ),
        migrations.AddIndex(
            model_name="whatsapptemplatetrackedlink",
            index=models.Index(
                fields=["meta_template_id", "sent_at"],
                name="wa_cta_link_tpl_sent_idx",
            ),
        ),
        migrations.AddIndex(
            model_name="whatsapptemplatetrackedclick",
            index=models.Index(
                fields=["link", "event_type", "created_at"],
                name="wa_cta_click_link_evt_idx",
            ),
        ),
    ]
