import uuid

from django.conf import settings
from django.db import migrations, models
import django.db.models.deletion
import django.core.validators


class Migration(migrations.Migration):

    initial = True

    dependencies = [
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
        ("organizations", "0001_initial"),
        ("crm", "0001_initial"),
    ]

    operations = [
        migrations.CreateModel(
            name="SalesDocumentNumberSequence",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("document_type", models.CharField(choices=[("quotation", "Quotation"), ("agreement", "Agreement"), ("invoice", "Invoice")], max_length=16)),
                ("next_number", models.PositiveBigIntegerField(default=1)),
                ("organization", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name="sales_number_sequences", to="organizations.organization")),
            ],
        ),
        migrations.CreateModel(
            name="SalesTemplate",
            fields=[
                ("id", models.UUIDField(default=uuid.uuid4, editable=False, primary_key=True, serialize=False)),
                ("document_type", models.CharField(choices=[("quotation", "Quotation"), ("agreement", "Agreement"), ("invoice", "Invoice")], max_length=16)),
                ("name", models.CharField(max_length=120)),
                ("number_prefix", models.CharField(blank=True, max_length=12)),
                ("logo_url", models.URLField(blank=True, max_length=2048)),
                ("signature_url", models.URLField(blank=True, max_length=2048)),
                ("accent_color", models.CharField(default="#0071e3", max_length=7, validators=[django.core.validators.RegexValidator(message="Use a six-digit hex color such as #0071e3.", regex="^#[0-9A-Fa-f]{6}$")])),
                ("header_text", models.CharField(blank=True, max_length=255)),
                ("body_template", models.TextField()),
                ("footer_text", models.TextField(blank=True)),
                ("email_subject_template", models.CharField(blank=True, max_length=255)),
                ("email_body_template", models.TextField(blank=True)),
                ("whatsapp_body_template", models.TextField(blank=True)),
                ("is_default", models.BooleanField(default=False)),
                ("is_active", models.BooleanField(default=True)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                ("created_by", models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name="sales_templates_created", to=settings.AUTH_USER_MODEL)),
                ("organization", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name="sales_templates", to="organizations.organization")),
            ],
            options={"ordering": ["document_type", "name"]},
        ),
        migrations.CreateModel(
            name="SalesDocument",
            fields=[
                ("id", models.UUIDField(default=uuid.uuid4, editable=False, primary_key=True, serialize=False)),
                ("document_type", models.CharField(choices=[("quotation", "Quotation"), ("agreement", "Agreement"), ("invoice", "Invoice")], max_length=16)),
                ("document_number", models.CharField(max_length=64)),
                ("title", models.CharField(max_length=180)),
                ("status", models.CharField(choices=[("draft", "Draft"), ("sent", "Sent"), ("accepted", "Accepted"), ("declined", "Declined"), ("signed", "Signed"), ("unpaid", "Unpaid"), ("partial", "Partially paid"), ("paid", "Paid"), ("overdue", "Overdue"), ("cancelled", "Cancelled")], default="draft", max_length=16)),
                ("recipient_name", models.CharField(blank=True, max_length=180)),
                ("recipient_email", models.EmailField(blank=True, max_length=254)),
                ("recipient_phone", models.CharField(blank=True, max_length=40)),
                ("currency", models.CharField(default="INR", max_length=8)),
                ("issue_date", models.DateField()),
                ("valid_until", models.DateField(blank=True, null=True)),
                ("due_date", models.DateField(blank=True, null=True)),
                ("line_items", models.JSONField(blank=True, default=list)),
                ("subtotal", models.DecimalField(decimal_places=2, default=0, max_digits=14)),
                ("tax_total", models.DecimalField(decimal_places=2, default=0, max_digits=14)),
                ("discount_total", models.DecimalField(decimal_places=2, default=0, max_digits=14)),
                ("total", models.DecimalField(decimal_places=2, default=0, max_digits=14)),
                ("content", models.TextField(blank=True)),
                ("terms", models.TextField(blank=True)),
                ("presentation_snapshot", models.JSONField(blank=True, default=dict)),
                ("rendered_html", models.TextField(blank=True)),
                ("email_subject_snapshot", models.CharField(blank=True, max_length=255)),
                ("email_body_snapshot", models.TextField(blank=True)),
                ("whatsapp_body_snapshot", models.TextField(blank=True)),
                ("public_token", models.UUIDField(default=uuid.uuid4, editable=False, unique=True)),
                ("accepted_by_name", models.CharField(blank=True, max_length=180)),
                ("accepted_by_email", models.EmailField(blank=True, max_length=254)),
                ("accepted_ip", models.GenericIPAddressField(blank=True, null=True)),
                ("sent_at", models.DateTimeField(blank=True, null=True)),
                ("accepted_at", models.DateTimeField(blank=True, null=True)),
                ("signed_at", models.DateTimeField(blank=True, null=True)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                ("created_by", models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name="sales_documents_created", to=settings.AUTH_USER_MODEL)),
                ("lead", models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name="sales_documents", to="crm.lead")),
                ("organization", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name="sales_documents", to="organizations.organization")),
                ("template", models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name="documents", to="sales.salestemplate")),
            ],
            options={"ordering": ["-created_at"]},
        ),
        migrations.CreateModel(
            name="SalesDocumentDelivery",
            fields=[
                ("id", models.UUIDField(default=uuid.uuid4, editable=False, primary_key=True, serialize=False)),
                ("channel", models.CharField(choices=[("email", "Email"), ("whatsapp", "WhatsApp")], max_length=12)),
                ("status", models.CharField(choices=[("queued", "Queued"), ("sent", "Sent"), ("failed", "Failed")], default="queued", max_length=12)),
                ("from_identity", models.CharField(blank=True, max_length=255)),
                ("to_identity", models.CharField(blank=True, max_length=255)),
                ("subject", models.CharField(blank=True, max_length=255)),
                ("body", models.TextField(blank=True)),
                ("provider_message_id", models.CharField(blank=True, max_length=255)),
                ("error_message", models.TextField(blank=True)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("sent_at", models.DateTimeField(blank=True, null=True)),
                ("document", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name="deliveries", to="sales.salesdocument")),
                ("organization", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name="sales_document_deliveries", to="organizations.organization")),
                ("sent_by", models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name="sales_deliveries_sent", to=settings.AUTH_USER_MODEL)),
            ],
            options={"ordering": ["-created_at"]},
        ),
        migrations.AddConstraint(
            model_name="salesdocumentnumbersequence",
            constraint=models.UniqueConstraint(fields=("organization", "document_type"), name="sales_sequence_org_type_uniq"),
        ),
        migrations.AddConstraint(
            model_name="salestemplate",
            constraint=models.UniqueConstraint(fields=("organization", "document_type", "name"), name="sales_template_org_type_name_uniq"),
        ),
        migrations.AddIndex(
            model_name="salestemplate",
            index=models.Index(fields=["organization", "document_type", "is_active"], name="sales_tpl_org_type_active"),
        ),
        migrations.AddConstraint(
            model_name="salesdocument",
            constraint=models.UniqueConstraint(fields=("organization", "document_type", "document_number"), name="sales_doc_org_type_number_uniq"),
        ),
        migrations.AddIndex(
            model_name="salesdocument",
            index=models.Index(fields=["organization", "document_type", "status", "created_at"], name="sales_doc_org_type_status"),
        ),
        migrations.AddIndex(
            model_name="salesdocument",
            index=models.Index(fields=["organization", "lead", "created_at"], name="sales_doc_org_lead_created"),
        ),
        migrations.AddIndex(
            model_name="salesdocumentdelivery",
            index=models.Index(fields=["organization", "channel", "status", "created_at"], name="sales_delivery_org_channel"),
        ),
    ]
