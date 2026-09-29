"""Authenticated capability discovery outside the JSON-RPC transport."""

from django.http import JsonResponse
from django.views.decorators.http import require_GET

from apps.integrations.diagnostic_auth import sanitize_text
from apps.integrations.operations.capabilities import capability_discovery
from apps.integrations.operations_auth import OperationsAuthError, authenticate_bearer
from apps.integrations.operations_endpoints import operations_resource_metadata_url
from apps.integrations.operations.tool_catalog import TOOL_CAPABILITIES, TOOL_DEFINITIONS


@require_GET
def operations_capabilities(request):
    auth = request.headers.get("Authorization", "")
    bearer = auth[7:].strip() if auth.lower().startswith("bearer ") else ""
    if not bearer:
        response = JsonResponse({"error": "authentication_required", "message": "Connect an actor-bound SHVYA Operations OAuth session first."}, status=401)
        response["WWW-Authenticate"] = f'Bearer resource_metadata="{operations_resource_metadata_url()}"'
        return response
    try:
        identity = authenticate_bearer(bearer)
    except OperationsAuthError as exc:
        response = JsonResponse({"error": "invalid_token", "message": sanitize_text(exc, limit=200)}, status=401)
        response["WWW-Authenticate"] = f'Bearer resource_metadata="{operations_resource_metadata_url()}", error="invalid_token"'
        return response
    tools = [{"name": item["name"], "capability": TOOL_CAPABILITIES.get(item["name"]), "securitySchemes": item.get("securitySchemes", [])} for item in TOOL_DEFINITIONS]
    payload = capability_discovery(identity=identity, tools=tools)
    payload.update({"endpoint": "/operations/capabilities", "context_first": "Call get_operations_context before reading or changing organization data."})
    response = JsonResponse(payload)
    response["Cache-Control"] = "no-store"
    return response
