import uuid

import django.db.models.deletion
from django.conf import settings
from django.db import migrations, models

import apps.channels.models


class Migration(migrations.Migration):
    dependencies = [
        ("channels", "0011_hostedchatignorecontact"),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.CreateModel(
            name="InstagramAccount",
            fields=[
                ("id", models.UUIDField(default=uuid.uuid4, editable=False, primary_key=True, serialize=False)),
                ("ig_user_id", models.CharField(max_length=80, unique=True)),
                ("username", models.CharField(blank=True, max_length=150)),
                ("display_name", models.CharField(blank=True, max_length=200)),
                ("account_type", models.CharField(blank=True, max_length=40)),
                ("profile_picture_url", models.URLField(blank=True, max_length=1000)),
                ("access_token", apps.channels.models.EncryptedTextField(blank=True)),
                ("token_expires_at", models.DateTimeField(blank=True, null=True)),
                ("token_refreshed_at", models.DateTimeField(blank=True, null=True)),
                ("status", models.CharField(choices=[("pending", "Pending"), ("connected", "Connected"), ("expired", "Token Expired"), ("revoked", "Access Revoked"), ("error", "Error"), ("disconnected", "Disconnected")], default="pending", max_length=20)),
                ("webhook_subscribed", models.BooleanField(default=False)),
                ("subscribed_fields", models.JSONField(blank=True, default=list)),
                ("connected_at", models.DateTimeField(blank=True, null=True)),
                ("last_webhook_at", models.DateTimeField(blank=True, null=True)),
                ("last_sync_at", models.DateTimeField(blank=True, null=True)),
                ("last_error", models.TextField(blank=True)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                ("connected_by", models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name="instagram_connections", to=settings.AUTH_USER_MODEL)),
                ("organization", models.OneToOneField(on_delete=django.db.models.deletion.CASCADE, related_name="instagram_account", to="organizations.organization")),
            ],
        ),
        migrations.CreateModel(
            name="InstagramOAuthAttempt",
            fields=[
                ("id", models.UUIDField(default=uuid.uuid4, editable=False, primary_key=True, serialize=False)),
                ("authorization_code", apps.channels.models.EncryptedTextField(blank=True)),
                ("redirect_uri", models.URLField(max_length=1000)),
                ("status", models.CharField(choices=[("queued", "Queued"), ("processing", "Processing"), ("connected", "Connected"), ("failed", "Failed")], default="queued", max_length=16)),
                ("error_message", models.TextField(blank=True)),
                ("expires_at", models.DateTimeField(blank=True, null=True)),
                ("completed_at", models.DateTimeField(blank=True, null=True)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                ("created_by", models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name="instagram_oauth_attempts", to=settings.AUTH_USER_MODEL)),
                ("organization", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name="instagram_oauth_attempts", to="organizations.organization")),
            ],
            options={"ordering": ["-created_at"]},
        ),
        migrations.CreateModel(
            name="InstagramConversation",
            fields=[
                ("id", models.UUIDField(default=uuid.uuid4, editable=False, primary_key=True, serialize=False)),
                ("meta_conversation_id", models.CharField(blank=True, max_length=160, null=True, unique=True)),
                ("participant_id", models.CharField(max_length=160)),
                ("participant_username", models.CharField(blank=True, max_length=150)),
                ("participant_name", models.CharField(blank=True, max_length=200)),
                ("participant_profile_picture_url", models.URLField(blank=True, max_length=1000)),
                ("last_message_text", models.TextField(blank=True)),
                ("last_message_at", models.DateTimeField(blank=True, null=True)),
                ("last_direction", models.CharField(blank=True, max_length=10)),
                ("unread_count", models.PositiveIntegerField(default=0)),
                ("last_synced_at", models.DateTimeField(blank=True, null=True)),
                ("raw_payload", models.JSONField(blank=True, default=dict)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                ("account", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name="conversations", to="channels.instagramaccount")),
                ("organization", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name="instagram_conversations", to="organizations.organization")),
            ],
            options={"ordering": ["-last_message_at", "-updated_at"]},
        ),
        migrations.CreateModel(
            name="InstagramMessage",
            fields=[
                ("id", models.UUIDField(default=uuid.uuid4, editable=False, primary_key=True, serialize=False)),
                ("external_id", models.CharField(blank=True, max_length=300, null=True, unique=True)),
                ("idempotency_key", models.UUIDField(default=uuid.uuid4, editable=False, unique=True)),
                ("direction", models.CharField(choices=[("inbound", "Inbound"), ("outbound", "Outbound")], max_length=10)),
                ("status", models.CharField(choices=[("queued", "Queued"), ("sent", "Sent"), ("received", "Received"), ("read", "Read"), ("failed", "Failed")], max_length=12)),
                ("message_type", models.CharField(choices=[("text", "Text"), ("image", "Image"), ("audio", "Audio"), ("video", "Video"), ("share", "Share"), ("sticker", "Sticker"), ("postback", "Postback"), ("unknown", "Unknown")], default="text", max_length=16)),
                ("sender_id", models.CharField(blank=True, max_length=160)),
                ("recipient_id", models.CharField(blank=True, max_length=160)),
                ("body", models.TextField(blank=True)),
                ("attachments", models.JSONField(blank=True, default=list)),
                ("raw_payload", models.JSONField(blank=True, default=dict)),
                ("error", models.TextField(blank=True)),
                ("is_read", models.BooleanField(default=True)),
                ("sent_at", models.DateTimeField(blank=True, null=True)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                ("account", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name="messages", to="channels.instagramaccount")),
                ("conversation", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name="messages", to="channels.instagramconversation")),
                ("organization", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name="instagram_messages", to="organizations.organization")),
            ],
            options={"ordering": ["created_at", "id"]},
        ),
        migrations.CreateModel(
            name="InstagramWebhookDelivery",
            fields=[
                ("id", models.UUIDField(default=uuid.uuid4, editable=False, primary_key=True, serialize=False)),
                ("payload_sha256", models.CharField(max_length=64, unique=True)),
                ("raw_payload", models.JSONField(default=dict)),
                ("status", models.CharField(choices=[("pending", "Pending"), ("processing", "Processing"), ("processed", "Processed"), ("ignored", "Ignored"), ("failed", "Failed")], default="pending", max_length=12)),
                ("error_message", models.TextField(blank=True)),
                ("received_at", models.DateTimeField(auto_now_add=True)),
                ("processed_at", models.DateTimeField(blank=True, null=True)),
            ],
            options={"ordering": ["-received_at"]},
        ),
        migrations.AddIndex(model_name="instagramaccount", index=models.Index(fields=["organization", "status"], name="ig_acct_org_status_idx")),
        migrations.AddIndex(model_name="instagramaccount", index=models.Index(fields=["token_expires_at"], name="ig_acct_token_exp_idx")),
        migrations.AddIndex(model_name="instagramoauthattempt", index=models.Index(fields=["organization", "created_at"], name="ig_oauth_org_created_idx")),
        migrations.AddIndex(model_name="instagramconversation", index=models.Index(fields=["organization", "last_message_at"], name="ig_conv_org_last_idx")),
        migrations.AddConstraint(model_name="instagramconversation", constraint=models.UniqueConstraint(fields=("account", "participant_id"), name="uniq_ig_account_participant")),
        migrations.AddIndex(model_name="instagrammessage", index=models.Index(fields=["organization", "created_at"], name="ig_msg_org_created_idx")),
        migrations.AddIndex(model_name="instagrammessage", index=models.Index(fields=["conversation", "created_at"], name="ig_msg_conv_created_idx")),
        migrations.AddIndex(model_name="instagramwebhookdelivery", index=models.Index(fields=["status", "received_at"], name="ig_hook_status_recv_idx")),
    ]
