import uuid

import django.db.models.deletion
from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("crm", "0023_alter_lead_lead_source"),
        ("integrations", "0003_googlesheetintegration"),
    ]

    operations = [
        migrations.CreateModel(
            name="MetaLeadPage",
            fields=[
                ("id", models.UUIDField(default=uuid.uuid4, editable=False, primary_key=True, serialize=False)),
                ("page_id", models.CharField(max_length=80)),
                ("page_name", models.CharField(blank=True, max_length=150)),
                ("encrypted_page_access_token", models.TextField()),
                ("encrypted_app_secret", models.TextField(blank=True)),
                ("is_active", models.BooleanField(default=True)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                ("organization", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name="meta_lead_pages", to="organizations.organization")),
            ],
            options={"constraints": [models.UniqueConstraint(fields=("organization", "page_id"), name="uniq_meta_page_org")]},
        ),
        migrations.CreateModel(
            name="MetaLeadForm",
            fields=[
                ("id", models.UUIDField(default=uuid.uuid4, editable=False, primary_key=True, serialize=False)),
                ("form_id", models.CharField(max_length=100)),
                ("form_name", models.CharField(max_length=200)),
                ("field_mapping", models.JSONField(blank=True, default=dict)),
                ("is_active", models.BooleanField(default=True)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                ("page", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name="forms", to="integrations.metaleadpage")),
                ("pipeline", models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name="meta_lead_forms", to="crm.pipeline")),
                ("stage", models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name="meta_lead_forms", to="crm.stage")),
            ],
            options={"ordering": ["form_name"], "constraints": [models.UniqueConstraint(fields=("page", "form_id"), name="uniq_meta_form_page")]},
        ),
    ]
