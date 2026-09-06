import json

from django.core.exceptions import ValidationError
from django.http import HttpResponseForbidden, JsonResponse
from django.shortcuts import get_object_or_404, render
from django.views.decorators.http import require_http_methods

from apps.crm.decorators import crm_login_required
from apps.triggers.models import SmartTrigger, TriggerRun
from services.triggers.rules import catalog, reorder, save_rule


@crm_login_required
@require_http_methods(["GET"])
def dashboard(request):
    return render(
        request,
        "triggers/dashboard.html",
        {
            "trigger_catalog": catalog(request.crm_user.organization),
            "trigger_admin": request.crm_user.role == "admin",
        },
    )


@crm_login_required
@require_http_methods(["GET", "POST", "PUT", "DELETE"])
def rules_api(request, rule_id=None):
    user = request.crm_user
    if request.method != "GET" and user.role != "admin":
        return HttpResponseForbidden(
            "Only organization admins can manage Smart Triggers."
        )
    rules = SmartTrigger.objects.filter(organization=user.organization)
    rule = get_object_or_404(rules, id=rule_id) if rule_id else None
    if request.method == "GET":
        return JsonResponse(
            {
                "rules": list(
                    rules.values(
                        "id",
                        "name",
                        "enabled",
                        "position",
                        "trigger_type",
                        "conditions",
                        "action_type",
                        "action",
                    )
                )
            }
        )
    if request.method == "DELETE":
        if not rule:
            return JsonResponse({"error": "Select a rule."}, status=400)
        rule.delete()
        return JsonResponse({"ok": True})
    try:
        data = json.loads(request.body)
        if request.method == "PUT" and not rule:
            return JsonResponse({"error": "Select a rule."}, status=400)
        result = save_rule(user, data, rule.id if rule else None)
        return JsonResponse({"id": str(result.id)})
    except (json.JSONDecodeError, UnicodeDecodeError):
        return JsonResponse({"error": "Invalid JSON."}, status=400)
    except ValidationError as exc:
        return JsonResponse({"error": " ".join(exc.messages)}, status=400)


@crm_login_required
@require_http_methods(["POST"])
def reorder_api(request):
    if request.crm_user.role != "admin":
        return HttpResponseForbidden("Only admins can reorder rules.")
    try:
        data = json.loads(request.body)
        reorder(request.crm_user, data.get("ids") if isinstance(data, dict) else None)
        return JsonResponse({"ok": True})
    except (json.JSONDecodeError, UnicodeDecodeError):
        return JsonResponse({"error": "Invalid JSON."}, status=400)
    except ValidationError as exc:
        return JsonResponse({"error": " ".join(exc.messages)}, status=400)


@crm_login_required
@require_http_methods(["GET"])
def history_api(request):
    runs = (
        TriggerRun.objects.filter(rule__organization=request.crm_user.organization)
        .select_related("rule", "lead")
        .order_by("-created_at")[:100]
    )
    return JsonResponse(
        {
            "runs": [
                {
                    "id": str(r.id),
                    "rule": r.rule.name,
                    "lead": r.lead.name,
                    "status": r.status,
                    "detail": r.detail,
                    "created_at": r.created_at,
                    "due_at": r.due_at,
                }
                for r in runs
            ]
        }
    )
