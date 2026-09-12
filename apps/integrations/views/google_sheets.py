from __future__ import annotations

import hmac
import json
import re
import secrets

from django.contrib import messages
from django.db.models import Prefetch
from django.http import HttpResponseNotAllowed, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone
from django.views.decorators.csrf import csrf_exempt

from apps.crm.authentication import crm_login_required
from apps.crm.models import AttributeDefinition, Pipeline, Stage
from apps.integrations.models import GoogleSheetIntegration
from apps.integrations.services.google_sheets import (
    CORE_TARGETS,
    MAX_ROWS_PER_WEBHOOK,
    build_google_apps_script,
    sanitize_headers,
)
from apps.integrations.tasks import process_google_sheet_rows_task


def _selected_pipeline_and_stage(*, organization, pipeline_id, stage_id):
    pipeline = get_object_or_404(
        Pipeline,
        id=pipeline_id,
        organization=organization,
        is_active=True,
    )
    stage = get_object_or_404(
        Stage,
        id=stage_id,
        pipeline=pipeline,
        is_active=True,
    )
    return pipeline, stage


def _extract_spreadsheet_id(value: str) -> str:
    value = str(value or "").strip()
    if not value:
        return ""
    match = re.search(r"/spreadsheets/d/([a-zA-Z0-9_-]+)", value)
    if match:
        return match.group(1)
    if re.fullmatch(r"[a-zA-Z0-9_-]{20,}", value):
        return value
    return ""


def _redirect_to_integration(integration_id=None, *, new=False):
    url = reverse("crm-connect-hub-google-sheets")
    if new:
        return redirect(f"{url}?new=1")
    if integration_id:
        return redirect(f"{url}?integration={integration_id}")
    return redirect(url)


@crm_login_required
def google_sheets_view(request):
    organization = request.crm_user.organization

    if request.method == "POST":
        action = request.POST.get("action", "").strip()

        if action == "create":
            pipeline, stage = _selected_pipeline_and_stage(
                organization=organization,
                pipeline_id=request.POST.get("pipeline_id"),
                stage_id=request.POST.get("stage_id"),
            )
            raw_spreadsheet = request.POST.get("spreadsheet_url", "").strip()
            integration = GoogleSheetIntegration(
                organization=organization,
                name=(request.POST.get("name", "").strip() or "Google Sheets Leads")[:120],
                spreadsheet_url=(
                    raw_spreadsheet
                    if raw_spreadsheet.startswith(("http://", "https://"))
                    else ""
                ),
                spreadsheet_id=_extract_spreadsheet_id(raw_spreadsheet),
                worksheet_name=(
                    request.POST.get("worksheet_name", "").strip() or "Sheet1"
                )[:180],
                pipeline=pipeline,
                stage=stage,
                import_existing=request.POST.get("import_existing") == "on",
            )
            integration.set_secret(secrets.token_urlsafe(32))
            integration.full_clean()
            integration.save()
            messages.success(
                request,
                "Google Sheets integration created. Install the Apps Script, run setup once, then map the detected columns.",
            )
            return _redirect_to_integration(integration.id)

        integration = get_object_or_404(
            GoogleSheetIntegration,
            id=request.POST.get("integration_id"),
            organization=organization,
        )

        if action == "update":
            pipeline, stage = _selected_pipeline_and_stage(
                organization=organization,
                pipeline_id=request.POST.get("pipeline_id"),
                stage_id=request.POST.get("stage_id"),
            )
            raw_spreadsheet = request.POST.get("spreadsheet_url", "").strip()
            integration.name = (
                request.POST.get("name", "").strip() or integration.name
            )[:120]
            integration.spreadsheet_url = (
                raw_spreadsheet
                if raw_spreadsheet.startswith(("http://", "https://"))
                else ""
            )
            parsed_id = _extract_spreadsheet_id(raw_spreadsheet)
            if parsed_id:
                integration.spreadsheet_id = parsed_id
            integration.worksheet_name = (
                request.POST.get("worksheet_name", "").strip() or "Sheet1"
            )[:180]
            integration.pipeline = pipeline
            integration.stage = stage
            integration.import_existing = request.POST.get("import_existing") == "on"
            integration.full_clean()
            integration.save()
            messages.success(request, "Google Sheets settings updated.")
            return _redirect_to_integration(integration.id)

        if action == "save_mapping":
            headers = (
                integration.discovered_headers
                if isinstance(integration.discovered_headers, list)
                else []
            )
            mapping = {}
            allowed_attribute_ids = {
                str(value)
                for value in AttributeDefinition.objects.filter(
                    organization=organization
                ).values_list("id", flat=True)
            }
            for index, header in enumerate(headers):
                target = request.POST.get(f"map_{index}", "").strip()
                if not target:
                    continue
                if target in CORE_TARGETS:
                    mapping[header] = target
                elif (
                    target.startswith("attribute:")
                    and target.split(":", 1)[1] in allowed_attribute_ids
                ):
                    mapping[header] = target

            if "core:phone" not in mapping.values():
                messages.error(
                    request,
                    "Map one Google Sheet column to Phone before activating sync.",
                )
                return _redirect_to_integration(integration.id)

            integration.mapping = mapping
            integration.is_enabled = request.POST.get("is_enabled") == "on"
            integration.last_error = ""
            integration.save(
                update_fields=["mapping", "is_enabled", "last_error", "updated_at"]
            )
            messages.success(
                request,
                "Column mapping saved. Real-time sync is active."
                if integration.is_enabled
                else "Column mapping saved.",
            )
            return _redirect_to_integration(integration.id)

        if action == "toggle":
            if (
                not integration.is_enabled
                and "core:phone" not in (integration.mapping or {}).values()
            ):
                messages.error(request, "Map a Phone column before turning on sync.")
            else:
                integration.is_enabled = not integration.is_enabled
                integration.save(update_fields=["is_enabled", "updated_at"])
                messages.success(
                    request,
                    "Sync enabled." if integration.is_enabled else "Sync paused.",
                )
            return _redirect_to_integration(integration.id)

        if action == "rotate_secret":
            integration.set_secret(secrets.token_urlsafe(32))
            integration.is_enabled = False
            integration.save(
                update_fields=["encrypted_secret", "is_enabled", "updated_at"]
            )
            messages.success(
                request,
                "Sync secret rotated and integration paused. Replace the Apps Script with the newly generated version, run setup, then re-enable sync.",
            )
            return _redirect_to_integration(integration.id)

        if action == "delete":
            name = integration.name
            integration.delete()
            messages.success(
                request,
                f"'{name}' removed from Google Sheets integrations.",
            )
            return _redirect_to_integration()

        messages.error(request, "Unknown Google Sheets action.")
        return _redirect_to_integration(integration.id)

    integrations = list(
        GoogleSheetIntegration.objects.filter(organization=organization)
        .select_related("pipeline", "stage")
        .order_by("-created_at")
    )
    selected_id = request.GET.get("integration", "").strip()
    selected = next(
        (item for item in integrations if str(item.id) == selected_id),
        None,
    )
    show_create = request.GET.get("new") == "1" or not integrations
    if selected is None and integrations and not show_create:
        selected = integrations[0]

    stages_qs = Stage.objects.filter(is_active=True).order_by("display_order", "name")
    pipelines = list(
        Pipeline.objects.filter(organization=organization, is_active=True)
        .prefetch_related(Prefetch("stages", queryset=stages_qs))
        .order_by("name")
    )
    attributes = list(
        AttributeDefinition.objects.filter(organization=organization).order_by(
            "display_order", "name"
        )
    )

    script = ""
    manifest = ""
    mapping_rows = []
    if selected is not None:
        webhook_url = request.build_absolute_uri(
            reverse(
                "google-sheets-ingest",
                kwargs={"token": selected.webhook_token},
            )
        )
        script = build_google_apps_script(
            integration=selected,
            webhook_url=webhook_url,
        )
        manifest = json.dumps(
            {
                "timeZone": organization.timezone or "Asia/Kolkata",
                "dependencies": {},
                "oauthScopes": [
                    "https://www.googleapis.com/auth/spreadsheets.currentonly",
                    "https://www.googleapis.com/auth/script.external_request",
                    "https://www.googleapis.com/auth/script.scriptapp",
                ],
                "runtimeVersion": "V8",
            },
            indent=2,
        )
        current_mapping = (
            selected.mapping if isinstance(selected.mapping, dict) else {}
        )
        mapping_rows = [
            {"header": header, "selected": current_mapping.get(header, "")}
            for header in (selected.discovered_headers or [])
        ]

    return render(
        request,
        "integrations/google_sheets.html",
        {
            "integrations": integrations,
            "selected": selected,
            "show_create": show_create,
            "pipelines": pipelines,
            "attributes": attributes,
            "mapping_rows": mapping_rows,
            "apps_script": script,
            "manifest_json": manifest,
        },
    )


@csrf_exempt
def google_sheets_ingest_view(request, token):
    if request.method != "POST":
        return HttpResponseNotAllowed(["POST"])

    integration = (
        GoogleSheetIntegration.objects.select_related(
            "organization", "pipeline", "stage"
        )
        .filter(webhook_token=token)
        .first()
    )
    if integration is None:
        return JsonResponse(
            {"success": False, "message": "Integration not found."},
            status=404,
        )

    supplied_secret = request.META.get("HTTP_X_SHVYA_SHEETS_SECRET", "")
    expected_secret = integration.get_secret()
    if (
        not supplied_secret
        or not expected_secret
        or not hmac.compare_digest(supplied_secret, expected_secret)
    ):
        return JsonResponse(
            {"success": False, "message": "Invalid integration secret."},
            status=403,
        )

    try:
        payload = json.loads(request.body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return JsonResponse(
            {"success": False, "message": "Invalid JSON payload."},
            status=400,
        )

    event = str(payload.get("event") or "").strip().lower()
    if event == "ping":
        return JsonResponse(
            {
                "success": True,
                "message": "SHVYA Google Sheets webhook is ready.",
            }
        )

    sheet_name = str(payload.get("sheet_name") or "").strip()
    if (
        sheet_name
        and sheet_name.casefold()
        != integration.worksheet_name.strip().casefold()
    ):
        return JsonResponse(
            {
                "success": False,
                "message": (
                    "This integration is configured for worksheet "
                    f"'{integration.worksheet_name}'."
                ),
            },
            status=409,
        )

    if event == "register":
        integration.spreadsheet_id = str(
            payload.get("spreadsheet_id") or integration.spreadsheet_id
        )[:255]
        spreadsheet_url = str(payload.get("spreadsheet_url") or "").strip()
        if spreadsheet_url.startswith(("http://", "https://")):
            integration.spreadsheet_url = spreadsheet_url[:2048]
        integration.sheet_id = str(payload.get("sheet_id") or "")[:64]
        integration.discovered_headers = sanitize_headers(payload.get("headers"))
        integration.last_registered_at = timezone.now()
        integration.last_error = ""
        integration.save(
            update_fields=[
                "spreadsheet_id",
                "spreadsheet_url",
                "sheet_id",
                "discovered_headers",
                "last_registered_at",
                "last_error",
                "updated_at",
            ]
        )
        return JsonResponse(
            {
                "success": True,
                "message": "Sheet registered. Return to SHVYA to map columns.",
                "headers": integration.discovered_headers,
            }
        )

    if event != "rows":
        return JsonResponse(
            {"success": False, "message": "Unsupported event."},
            status=400,
        )

    if not integration.is_enabled:
        return JsonResponse(
            {
                "success": False,
                "message": (
                    "Integration is paused. Save mapping and enable sync in SHVYA."
                ),
            },
            status=409,
        )

    rows = payload.get("rows")
    if not isinstance(rows, list) or not rows:
        return JsonResponse(
            {"success": False, "message": "No rows supplied."},
            status=400,
        )
    if len(rows) > MAX_ROWS_PER_WEBHOOK:
        return JsonResponse(
            {
                "success": False,
                "message": (
                    f"A maximum of {MAX_ROWS_PER_WEBHOOK} rows is allowed per request."
                ),
            },
            status=413,
        )

    process_google_sheet_rows_task.delay(str(integration.id), rows)
    return JsonResponse(
        {
            "success": True,
            "accepted": len(rows),
            "message": "Rows queued for CRM sync.",
        },
        status=202,
    )
