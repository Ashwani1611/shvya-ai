"""Authenticated Bulk Campaigns workspace and bounded, tenant-scoped JSON APIs."""
from __future__ import annotations

import csv
import json
from functools import wraps
from io import StringIO

from django.core.exceptions import PermissionDenied, ValidationError
from django.core.paginator import Paginator
from django.db import transaction
from django.db.models import Q
from django.http import Http404, JsonResponse, StreamingHttpResponse
from django.shortcuts import render
from django.urls import reverse
from django.utils import timezone
from django.views.decorators.http import require_GET, require_POST

from apps.channels.campaign_models import CampaignDelivery
from apps.channels.models import WhatsAppAccount, WhatsAppTemplate
from apps.crm.decorators import crm_login_required
from services.channels.campaign_audience import (
    create_upload, definitions, is_suppressed, owned_upload, require_manage,
    review_upload, rights, source_catalog, user_pipelines, uuid_value, visible_campaigns,
)
from services.channels.campaign_policy import CampaignInputError, csv_cell, template_fields
from services.channels.campaign_reporting import (
    campaign_report, delivery_counts, filter_deliveries, filter_legacy, iso,
    legacy_recipient_report, recipient_report,
)
from services.channels.campaign_service import (
    cancel_campaign, confirm_campaign, default_bindings, preview_campaign,
    retry_eligibility, retry_recipients, template_snapshot,
)


def api_errors(view):
    @wraps(view)
    def wrapped(request, *args, **kwargs):
        try:
            user_pipelines(request.crm_user)
            response = view(request, *args, **kwargs)
        except PermissionDenied:
            response = JsonResponse({"error": "This campaign, upload or action is not available to your account."}, status=403)
        except Http404:
            response = JsonResponse({"error": "The requested campaign record is not available."}, status=404)
        except (CampaignInputError, ValidationError) as exc:
            message = "; ".join(exc.messages) if isinstance(exc, ValidationError) else str(exc)
            response = JsonResponse({"error": message}, status=400)
        response["Cache-Control"] = "no-store, private"
        response["X-Content-Type-Options"] = "nosniff"
        return response
    return wrapped


def payload(request):
    if len(request.body) > 2 * 1024 * 1024:
        raise CampaignInputError("This request is too large. Use select all matching recipients instead.")
    try:
        data = json.loads(request.body or b"{}")
    except (ValueError, UnicodeDecodeError) as exc:
        raise CampaignInputError("The request is not valid JSON.") from exc
    if not isinstance(data, dict):
        raise CampaignInputError("The request must be an object.")
    return data


def get_campaign(user, campaign_id):
    campaign = visible_campaigns(user).select_related("account", "campaign_plan", "pipeline").filter(pk=campaign_id).first()
    if campaign is None:
        raise Http404
    return campaign


def paginate(rows, requested, size=30):
    try:
        number = int(requested or 1)
    except (ValueError, TypeError) as exc:
        raise CampaignInputError("Page must be a number.") from exc
    paginator = Paginator(rows, size)
    page = paginator.get_page(number)
    return page, {"number": page.number, "pages": paginator.num_pages, "total": paginator.count}


def _route(name, **kwargs):
    return reverse(f"whatsapp-campaign-{name}", kwargs=kwargs or None)


@crm_login_required
@require_GET
@api_errors
def workspace(request, campaign_id=None):
    if campaign_id:
        get_campaign(request.crm_user, campaign_id)
    urls = {key: _route(key) for key in ("list", "options", "upload", "templates", "preview", "confirm", "sample")}
    urls["data"] = _route("detail-data", campaign_id=campaign_id) if campaign_id else _route("list-data")
    if campaign_id:
        urls["actions"] = _route("actions", campaign_id=campaign_id)
    return render(request, "channels/bulk_campaigns.html", {
        "campaign_bootstrap": {"campaign_id": str(campaign_id) if campaign_id else None,
                               "open_composer": request.resolver_match.url_name == "whatsapp-campaign-create",
                               "urls": urls},
    })


@crm_login_required
@require_GET
@api_errors
def options(request):
    user = request.crm_user
    pipelines = list(user_pipelines(user).prefetch_related("stages"))
    accounts = WhatsAppAccount.objects.filter(
        organization_id=user.organization_id, connection_type="api", status="connected", is_active=True,
    ).exclude(phone_number_id="").exclude(access_token="").order_by("business_name", "pk")
    fields = [{"key": key, "label": label, "required": key in {"name", "phone"}}
              for key, label in (("name", "Name"), ("phone", "Phone"), ("email", "Email"), ("pipeline", "Pipeline"), ("stage", "Stage"))]
    fields.extend({"key": f"attr:{field.key}", "label": field.name, "required": False} for field in definitions(user))
    return JsonResponse({
        "pipelines": [{"id": str(p.pk), "name": p.name, "stages": [{"id": str(s.pk), "name": s.name}
                        for s in p.stages.all() if s.is_active]} for p in pipelines],
        "accounts": [{"id": str(a.pk), "name": " · ".join(filter(None, [a.business_name, a.display_phone_number])) or "WhatsApp",
                      "business_name": a.business_name, "phone": a.display_phone_number} for a in accounts],
        "fields": fields, "sources": source_catalog(user),
        "can_create": any(rights(user, p)["can_edit_leads"] for p in pipelines),
    })


@crm_login_required
@require_POST
@api_errors
def upload(request):
    user = request.crm_user
    if not any(rights(user, p)["can_edit_leads"] for p in user_pipelines(user)):
        raise PermissionDenied
    item = create_upload(user=user, uploaded_file=request.FILES.get("file"))
    return JsonResponse({"id": str(item.pk), "filename": item.filename, "row_count": len(item.rows), "headers": item.headers,
                         "review_url": _route("upload-review", upload_id=item.pk),
                         "errors_url": _route("upload-errors", upload_id=item.pk)}, status=201)


@crm_login_required
@require_POST
@api_errors
def review(request, upload_id):
    item = review_upload(user=request.crm_user, token=upload_id, data=payload(request))
    return JsonResponse({"stats": {key: value for key, value in item.review_stats.items() if key != "errors"},
                         "errors": item.review_stats.get("errors", [])[:25], "digest": item.review_digest})


def csv_response(rows, filename):
    def stream():
        buffer = StringIO()
        writer = csv.writer(buffer)
        yield "\ufeff"
        for row in rows:
            buffer.seek(0)
            buffer.truncate(0)
            writer.writerow([csv_cell(value) for value in row])
            yield buffer.getvalue()
    response = StreamingHttpResponse(stream(), content_type="text/csv; charset=utf-8")
    response["Content-Disposition"] = f'attachment; filename="{filename}"'
    return response


@crm_login_required
@require_GET
@api_errors
def sample(request):
    # Headers only: no synthetic recipients that could accidentally be sent.
    return csv_response([["Name", "Phone", "Email", "Pipeline", "Stage"]], "bulk-campaign-sample.csv")


@crm_login_required
@require_POST
@api_errors
def upload_errors(request, upload_id):
    item = owned_upload(user=request.crm_user, token=upload_id)
    rows = [["Spreadsheet row", "Outcome", "Reason"]]
    rows.extend([error["row"], error.get("kind", "excluded"), error["reason"]] for error in item.review_stats.get("errors", []))
    return csv_response(rows, "bulk-campaign-row-review.csv")


@crm_login_required
@require_GET
@api_errors
def templates(request):
    user = request.crm_user
    account_id = uuid_value(request.GET.get("account"), "Sending account")
    account = WhatsAppAccount.objects.filter(pk=account_id, organization_id=user.organization_id, connection_type="api",
                                             is_active=True, status="connected").first()
    if account is None:
        raise PermissionDenied
    query = WhatsAppTemplate.objects.filter(organization_id=user.organization_id, account=account, status="approved").exclude(meta_template_id="")
    if request.GET.get("category"):
        query = query.filter(category=request.GET["category"])
    if request.GET.get("q"):
        query = query.filter(Q(name__icontains=str(request.GET["q"])[:150]) | Q(body__icontains=str(request.GET["q"])[:150]))
    selected = list(query.select_related("meta_state").order_by("name", "pk")[:201])
    result, sources = [], source_catalog(user)
    for template in selected[:200]:
        row = {"id": str(template.pk), "name": template.name, "category": template.category,
               "body": template.body, "updated_at": iso(template.updated_at), "language": "",
               "fields": [], "bindings": {}, "supported": True, "reason": ""}
        try:
            spec = template_snapshot(template)
            row.update(language=spec["language"], fields=template_fields(spec), bindings=default_bindings(spec, sources))
        except CampaignInputError as exc:
            row.update(supported=False, reason=str(exc))
        result.append(row)
    return JsonResponse({"templates": result, "truncated": len(selected) > 200})


@crm_login_required
@require_POST
@api_errors
def preview(request):
    data = payload(request)
    item = owned_upload(user=request.crm_user, token=data.get("upload_id"))
    result = preview_campaign(user=request.crm_user, upload=item, template_id=data.get("template_id"), bindings=data.get("bindings"))
    result.pop("spec", None)
    return JsonResponse(result)


@crm_login_required
@require_POST
@api_errors
def confirm(request):
    campaign, created = confirm_campaign(user=request.crm_user, data=payload(request))
    return JsonResponse({"id": str(campaign.pk), "url": _route("detail", campaign_id=campaign.pk), "created": created}, status=201 if created else 200)


@crm_login_required
@require_GET
@api_errors
def list_data(request):
    user = request.crm_user
    campaigns = visible_campaigns(user).select_related("account", "campaign_plan")
    if request.GET.get("q"):
        campaigns = campaigns.filter(name__icontains=str(request.GET["q"])[:150])
    if request.GET.get("account"):
        campaigns = campaigns.filter(account_id=uuid_value(request.GET["account"], "Account filter"))
    active = campaigns.filter(status__in=["queued", "sending"])
    active_page, active_pagination = paginate(active.order_by("-created_at", "-pk"), request.GET.get("active_page"), size=20)
    history, pagination = paginate(campaigns.exclude(status__in=["queued", "sending"]).order_by("-created_at", "-pk"), request.GET.get("page"))
    return JsonResponse({
        "active": [campaign_report(campaign, user) for campaign in active_page],
        "active_count": active_pagination["total"], "active_pagination": active_pagination, "history": [campaign_report(campaign, user) for campaign in history],
        "summary": delivery_counts(CampaignDelivery.objects.filter(campaign__in=campaigns)),
        "pagination": pagination, "retrieved_at": iso(timezone.now()),
    })


def recipient_query(campaign, user, filters):
    if getattr(campaign, "campaign_plan", None):
        return filter_deliveries(campaign=campaign, user=user, filters=filters).select_related("lead__pipeline", "lead__stage")
    return filter_legacy(campaign=campaign, filters=filters)


@crm_login_required
@require_GET
@api_errors
def detail_data(request, campaign_id):
    user = request.crm_user
    campaign = get_campaign(user, campaign_id)
    plan = getattr(campaign, "campaign_plan", None)
    rows = recipient_query(campaign, user, request.GET)
    if plan:
        rows = rows.prefetch_related("attempts")
    page, pagination = paginate(rows.order_by("created_at", "pk"), request.GET.get("page"), size=50)
    recipients = []
    for item in page:
        row = recipient_report(item, campaign=campaign, plan=plan) if plan else legacy_recipient_report(item, campaign=campaign)
        row["lead_url"] = _route("lead", campaign_id=campaign.pk, recipient_id=item.pk)
        recipients.append(row)
    return JsonResponse({"campaign": campaign_report(campaign, user), "recipients": recipients,
                         "pagination": pagination, "retrieved_at": iso(timezone.now())})


def selected_rows(campaign, user, selection):
    if not isinstance(selection, dict):
        raise CampaignInputError("Choose recipients for this action.")
    if selection.get("all") is True:
        if "ids" in selection or not isinstance(selection.get("filters", {}), dict):
            raise CampaignInputError("Choose either explicit recipients or all matching recipients.")
        rows = recipient_query(campaign, user, selection.get("filters", {}))
    else:
        ids = selection.get("ids")
        if not isinstance(ids, list) or not ids or len(ids) > 25000:
            raise CampaignInputError("Choose between 1 and 25,000 recipients.")
        keys = {uuid_value(value, "Recipient") for value in ids}
        rows = recipient_query(campaign, user, {}).filter(pk__in=keys)
        if rows.count() != len(keys):
            raise CampaignInputError("Some recipients are no longer available in this campaign. Refresh your selection.")
    if rows.count() > 25000:
        raise CampaignInputError("Narrow the selection to at most 25,000 recipients.")
    return rows


def export_rows(campaign, user, rows):
    plan = getattr(campaign, "campaign_plan", None)
    attrs = definitions(user)
    def records():
        yield ["Name", "Phone", "Email", "Pipeline", "Stage", "Status", "Error code", "Error", "Attempts",
               "Accepted at", "Sent at", "Delivered at", "Read at", "Replied at"] + [field.name for field in attrs]
        for item in rows.order_by("pk").iterator(chunk_size=200):
            lead = item.lead
            if plan:
                state = "read" if item.read_at else "delivered" if item.delivered_at else item.state
                values = [item.name, item.phone, lead.email if lead else "", lead.pipeline.name if lead else item.pipeline_label,
                          lead.stage.name if lead else item.stage_label, state, item.error_code, item.error_message, item.attempt_count]
                values.extend(iso(getattr(item, f"{name}_at")) for name in ("accepted", "sent", "delivered", "read", "replied"))
            else:
                values = [lead.name, lead.phone, lead.email, lead.pipeline.name, lead.stage.name,
                          item.message.status if item.message else item.status, "", item.message.error if item.message else item.skip_reason,
                          "", "", "", "", "", ""]
            values.extend((lead.attributes or {}).get(field.key, "") if lead else item.values.get(f"attr:{field.key}", "") for field in attrs)
            yield values
    return csv_response(records(), f"bulk-campaign-{campaign.pk}.csv")


@crm_login_required
@require_POST
@api_errors
def actions(request, campaign_id):
    user, data = request.crm_user, payload(request)
    campaign = get_campaign(user, campaign_id)
    action = data.get("action")
    if action == "export":
        return export_rows(campaign, user, selected_rows(campaign, user, data.get("selection")))
    require_manage(user, campaign)
    plan = getattr(campaign, "campaign_plan", None)
    if not plan:
        raise CampaignInputError("Historical campaigns are read-only. Create a new campaign to send an approved template.")
    if action == "cancel":
        return JsonResponse({"cancelled_recipients": cancel_campaign(user=user, campaign=campaign)})
    if action == "rename":
        name = str(data.get("name") or "").strip()
        if not name or len(name) > 150:
            raise CampaignInputError("Enter a campaign name of 1–150 characters.")
        with transaction.atomic():
            campaign.name = name
            campaign.save(update_fields=["name"])
        return JsonResponse({"name": name})
    if action not in {"retry_preview", "retry"}:
        raise CampaignInputError("Choose a supported campaign action.")
    rows = selected_rows(campaign, user, data.get("selection"))
    if action == "retry":
        return JsonResponse(retry_recipients(user=user, campaign=campaign, selection=rows))
    eligible, blocked, earliest, reasons = 0, 0, None, []
    now = timezone.now()
    for item in rows.iterator(chunk_size=200):
        allowed, due, reason = retry_eligibility(item, plan, now=now)
        if allowed and is_suppressed(organization_id=user.organization_id, phone=item.phone, lead=item.lead):
            allowed, reason = False, "Recipient has opted out."
        if allowed:
            eligible += 1
            earliest = min(earliest, due) if earliest else due
        else:
            blocked += 1
            if len(reasons) < 20:
                reasons.append({"id": str(item.pk), "reason": reason})
    return JsonResponse({"selected": eligible + blocked, "eligible": eligible, "blocked": blocked, "earliest": iso(earliest), "reasons": reasons})


@crm_login_required
@require_GET
@api_errors
def lead_details(request, campaign_id, recipient_id):
    campaign = get_campaign(request.crm_user, campaign_id)
    item = recipient_query(campaign, request.crm_user, {}).filter(pk=recipient_id).first()
    if item is None or item.lead is None:
        raise Http404
    lead = item.lead
    return JsonResponse({"name": lead.name, "phone": lead.phone, "email": lead.email,
                         "pipeline": lead.pipeline.name, "stage": lead.stage.name,
                         "source": lead.get_lead_source_display(), "created_at": iso(lead.created_at),
                         "attributes": [{"label": field.name, "value": (lead.attributes or {}).get(field.key, "")}
                                        for field in definitions(request.crm_user)]})


@crm_login_required
@require_POST
@api_errors
def legacy_launch(request, campaign_id):
    get_campaign(request.crm_user, campaign_id)
    return JsonResponse({"error": "Use the Bulk Campaigns builder to review recipients, approve a template and confirm sending."}, status=409)
