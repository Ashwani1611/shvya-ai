import json

from django.core.exceptions import ValidationError
from django.http import JsonResponse
from django.shortcuts import render
from django.views.decorators.http import require_http_methods

from apps.integrations.access import connect_hub_admin_required
from apps.integrations.services.meta_conversions import (
    dashboard_state, eligible_test_leads, get_configuration, perform_action,
)


@connect_hub_admin_required
@require_http_methods(["GET", "POST"])
def meta_conversions_view(request):
    configuration = get_configuration(request.crm_user.organization)
    if request.method == "POST":
        try:
            if len(request.body) > 65536:
                raise ValidationError("The connection request is too large.")
            data = json.loads(request.body)
            if not isinstance(data, dict):
                raise ValidationError("Send a valid connection request.")
            message = perform_action(configuration, data)
        except (json.JSONDecodeError, UnicodeDecodeError):
            return JsonResponse({"error": "Send a valid JSON request."}, status=400)
        except ValidationError as exc:
            return JsonResponse({"error": "; ".join(exc.messages), "fields": getattr(exc, "message_dict", {})}, status=400)
        return JsonResponse({"message": message, "state": dashboard_state(configuration)})
    if request.GET.get("lookup") == "leads":
        try:
            response = JsonResponse({"leads": eligible_test_leads(configuration, request.GET.get("mapping_id"))})
        except ValidationError as exc:
            response = JsonResponse({"error": "; ".join(exc.messages)}, status=400)
        response["Cache-Control"] = "no-store"
        return response
    state = dashboard_state(configuration)
    if request.GET.get("format") == "json":
        response = JsonResponse(state)
        response["Cache-Control"] = "no-store"
        return response
    response = render(request, "integrations/meta_conversions.html", {"capi_state": state})
    response["Cache-Control"] = "no-store"
    return response
