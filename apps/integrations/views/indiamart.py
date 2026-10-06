import json
import re
import uuid
from django import forms
from django.contrib import messages
from django.core.exceptions import ValidationError
from django.db import transaction
from django.http import JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_http_methods, require_POST
from apps.crm.models import Pipeline, Stage, Lead, LeadNote
from apps.integrations.access import connect_hub_admin_required
from apps.integrations.models import IndiaMartConnection, IndiaMartReceipt
from apps.organizations.models import Organization
from apps.superadmin.feature_toggle_views import superuser_required
from apps.superadmin.models import AuditLog
from services.crm.lead_service import _schedule_new_lead_welcome, upsert_lead
from apps.crm.models.lead import normalize_phone


class RoutingForm(forms.Form):
    pipeline = forms.ModelChoiceField(
        queryset=Pipeline.objects.none(),
        label="Pipeline",
        empty_label="Select a pipeline",
    )
    stage = forms.ModelChoiceField(
        queryset=Stage.objects.none(),
        label="Stage",
        empty_label="Select a stage",
    )

    def __init__(self, *args, organization, **kwargs):
        super().__init__(*args, **kwargs)
        pipelines = Pipeline.objects.filter(organization=organization, is_active=True)
        self.fields["pipeline"].queryset = pipelines
        self.fields["pipeline"].label_from_instance = lambda pipeline: pipeline.name
        stages = Stage.objects.filter(pipeline__in=pipelines, is_active=True)
        self.stage_options = [
            {
                "id": str(stage.id),
                "pipeline": str(stage.pipeline_id),
                "name": stage.name,
            }
            for stage in stages
        ]
        pipeline_id = (
            self.data.get(self.add_prefix("pipeline"))
            if self.is_bound
            else self.initial.get("pipeline")
        )
        try:
            pipeline_id = uuid.UUID(str(pipeline_id))
        except (ValueError, TypeError, AttributeError):
            pipeline_id = None
        self.fields["stage"].queryset = stages.filter(pipeline_id=pipeline_id)
        self.fields["stage"].label_from_instance = lambda stage: stage.name
        for name, field in self.fields.items():
            field.widget.attrs.update(
                {
                    "class": "w-full border border-gray-200 rounded-xl p-3 bg-white text-sm focus:border-blue-500 focus:ring-2 focus:ring-blue-100",
                    "aria-describedby": f"id_{name}_help",
                }
            )


@connect_hub_admin_required
@require_http_methods(["GET", "POST"])
def indiamart_view(request):
    connection = IndiaMartConnection.objects.filter(
        organization=request.crm_user.organization
    ).first()
    if request.method == "POST":
        connection, _ = IndiaMartConnection.objects.get_or_create(
            organization=request.crm_user.organization
        )
        if not connection.requested_at:
            connection.requested_at = timezone.now()
            connection.save(update_fields=["requested_at"])
        messages.success(
            request,
            "IndiaMART setup requested. Our team will help you connect your seller account.",
        )
        return redirect("crm-connect-hub-indiamart")
    return render(request, "integrations/indiamart.html", {"connection": connection})


@superuser_required
@require_http_methods(["GET", "POST"])
def indiamart_setup_view(request, organization_id):
    organization = get_object_or_404(Organization, pk=organization_id)
    connection, _ = IndiaMartConnection.objects.get_or_create(organization=organization)
    form = RoutingForm(
        request.POST or None,
        organization=organization,
        initial={"pipeline": connection.pipeline_id, "stage": connection.stage_id},
    )
    status = 200
    if request.method == "POST":
        action = request.POST.get("action")
        if action == "disable":
            connection.is_enabled = False
            connection.save(update_fields=["is_enabled"])
        elif action in {"generate", "rotate"} and form.is_valid():
            connection.stage = form.cleaned_data["stage"]
            connection.pipeline = form.cleaned_data["pipeline"]
            if action == "rotate":
                connection.webhook_token = uuid.uuid4()
            connection.generated_at = timezone.now()
            connection.is_enabled = True
            connection.save()
        else:
            status = 400
        if status != 400:
            AuditLog.record(
                actor=request.user,
                action=AuditLog.Action.ORGANIZATION_UPDATED,
                target=organization,
                request=request,
                integration="indiamart",
                integration_action=action,
            )
            return redirect("superadmin-indiamart", organization_id=organization.id)
    webhook_url = (
        request.build_absolute_uri(
            reverse("indiamart-ingest", kwargs={"token": connection.webhook_token})
        )
        if connection.generated_at
        else ""
    )
    response = render(
        request,
        "superadmin/indiamart.html",
        {
            "organization": organization,
            "connection": connection,
            "form": form,
            "stage_options": form.stage_options,
            "webhook_url": webhook_url,
        },
        status=status,
    )
    response["Cache-Control"] = "no-store"
    # HTTPS CSRF checks require a same-origin Referer when a browser omits
    # Origin. Still prevent referrer disclosure to external destinations.
    response["Referrer-Policy"] = "same-origin"
    return response


@csrf_exempt
@require_POST
def indiamart_ingest_view(request, token):
    if len(request.body) > 262144:
        return JsonResponse({"error": "Payload too large"}, status=413)
    try:
        payload = json.loads(request.body)
        rows = payload.get("RESPONSE", payload) if isinstance(payload, dict) else None
        rows = rows if isinstance(rows, list) else [rows]
        if not rows or len(rows) > 100:
            raise ValidationError("Send between 1 and 100 enquiries.")
        with transaction.atomic():
            connection = get_object_or_404(
                IndiaMartConnection.objects.select_for_update(
                    of=("self",)
                ).select_related("organization", "pipeline", "stage"),
                webhook_token=token,
                is_enabled=True,
                generated_at__isnull=False,
                organization__is_active=True,
            )
            if (
                not connection.pipeline_id
                or not connection.stage_id
                or connection.stage.pipeline_id != connection.pipeline_id
                or connection.pipeline.organization_id != connection.organization_id
                or not connection.pipeline.is_active
            ):
                return JsonResponse(
                    {"error": "Lead routing needs attention"}, status=503
                )
            for row in rows:
                if not isinstance(row, dict):
                    raise ValidationError("Invalid enquiry.")
                query_id = str(row.get("UNIQUE_QUERY_ID") or "").strip()
                if not query_id or len(query_id) > 100:
                    raise ValidationError("A valid UNIQUE_QUERY_ID is required.")
                if connection.receipts.filter(query_id=query_id).exists():
                    continue
                raw_phone = str(
                    row.get("SENDER_MOBILE") or row.get("SENDER_PHONE") or ""
                ).strip()
                digits = re.sub(r"\D", "", raw_phone)
                if not raw_phone.startswith("+"):
                    if (
                        len(digits) == 10
                        and str(row.get("SENDER_COUNTRY_ISO", "")).upper() == "IN"
                    ):
                        digits = "91" + digits
                    elif len(digits) == 10:
                        raise ValidationError("Phone country code is required.")
                    raw_phone = "+" + digits
                if not 8 <= len(digits) <= 15:
                    raise ValidationError("Invalid buyer phone.")
                phone = normalize_phone(raw_phone)
                if not phone:
                    raise ValidationError("Buyer phone is required.")
                existing = (
                    Lead.objects.select_for_update()
                    .filter(organization=connection.organization, phone=phone)
                    .first()
                )
                text = "\n".join(
                    f"{label}: {row[key]}"
                    for key, label in [
                        ("UNIQUE_QUERY_ID", "IndiaMART enquiry"),
                        ("QUERY_TIME", "Enquiry time"),
                        ("QUERY_TYPE", "Type"),
                        ("SENDER_COMPANY", "Company"),
                        ("SUBJECT", "Subject"),
                        ("QUERY_PRODUCT_NAME", "Product"),
                        ("QUERY_MCAT_NAME", "Category"),
                        ("QUERY_MESSAGE", "Requirement"),
                        ("SENDER_ADDRESS", "Address"),
                        ("SENDER_CITY", "City"),
                        ("SENDER_STATE", "State"),
                        ("SENDER_COUNTRY_ISO", "Country"),
                    ]
                    if row.get(key)
                )
                if existing:
                    lead = existing
                    LeadNote.objects.create(lead=lead, note=text, note_type="system")
                else:
                    lead, _ = upsert_lead(
                        organization=connection.organization,
                        pipeline=connection.pipeline,
                        stage=connection.stage,
                        name=str(row.get("SENDER_NAME") or "IndiaMART Buyer")[:150],
                        phone=phone,
                        email=str(row.get("SENDER_EMAIL") or ""),
                        notes=text,
                        lead_source="indiamart",
                        send_welcome=False,
                    )
                    transaction.on_commit(
                        lambda lead=lead: _schedule_new_lead_welcome(lead), robust=True
                    )
                IndiaMartReceipt.objects.create(
                    connection=connection, query_id=query_id, lead=lead, payload=row
                )
            connection.last_received_at = timezone.now()
            connection.save(update_fields=["last_received_at"])
    except (ValueError, UnicodeDecodeError, ValidationError):
        return JsonResponse({"error": "Invalid IndiaMART enquiry payload."}, status=400)
    return JsonResponse({"CODE": 200, "STATUS": "SUCCESS"})
