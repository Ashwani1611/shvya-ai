"""Organization-scoped agent interface; tokens never grant production CRM writes."""
import json
import secrets
from functools import wraps

from django.core.exceptions import ObjectDoesNotExist, ValidationError
from django.http import HttpResponse, JsonResponse
from django.db import transaction
from django.utils import timezone

from .models import Vault
from django.views.decorators.csrf import csrf_exempt

from .services import (VaultConflict, authenticate_agent_token, call_payload,
                       entry_payload, export_markdown, question_payload,
                       upsert_call, upsert_entry, upsert_question, workspace_payload)

MAX_JSON_BYTES = 65536


def _error(message, status, code, details=None):
    payload = {"error": message, "code": code}
    if details:
        payload["details"] = details
    response = JsonResponse(payload, status=status)
    response["Cache-Control"] = "no-store"
    if status == 401:
        response["WWW-Authenticate"] = 'Bearer realm="SHVYA Vault"'
    return response


def agent_endpoint(method):
    def decorate(view):
        @csrf_exempt
        @wraps(view)
        def wrapped(request):
            if request.method != method:
                response = _error(f"Use {method} for this endpoint.", 405, "method_not_allowed")
                response["Allow"] = method
                return response
            auth = request.headers.get("Authorization", "")
            parts = auth.split()
            vault = authenticate_agent_token(parts[1]) if len(parts) == 2 and parts[0].lower() == "bearer" else None
            if vault is None:
                return _error("Vault token is missing, expired or revoked. Ask your SHVYA administrator for a fresh token.", 401, "invalid_token")
            request.vault = vault
            if method == "POST":
                if request.content_type != "application/json":
                    return _error("Send an application/json request body.", 415, "unsupported_media_type")
                try:
                    if int(request.META.get("CONTENT_LENGTH") or 0) > MAX_JSON_BYTES or len(request.body) > MAX_JSON_BYTES:
                        return _error("Request body exceeds 64 KiB.", 413, "body_too_large")
                    data = json.loads(request.body)
                    if not isinstance(data, dict):
                        raise ValueError
                except (ValueError, UnicodeDecodeError, json.JSONDecodeError):
                    return _error("Provide a valid JSON object.", 400, "invalid_json")
                if {"vault", "vault_id", "organization", "organization_id", "workspace", "workspace_id", "author_type", "client_body", "confirmed_at"} & data.keys():
                    return _error("Workspace and author are determined by your token; protected fields cannot be supplied.", 400, "protected_fields")
                request.vault_data = data
            try:
                if method == "POST":
                    with transaction.atomic():
                        current = Vault.objects.select_for_update().get(pk=vault.pk)
                        if not current.token_hash or not current.token_expires_at or current.token_expires_at <= timezone.now() or not secrets.compare_digest(current.token_hash, vault.token_hash):
                            return _error("Vault token expired or was revoked. Ask for a fresh token.", 401, "invalid_token")
                        request.vault = current
                        response = view(request)
                else:
                    response = view(request)
            except VaultConflict as exc:
                return _error(" ".join(exc.messages), 409, "conflict")
            except ValidationError as exc:
                details = exc.message_dict if hasattr(exc, "message_dict") else {"non_field_errors": exc.messages}
                return _error("Check the submitted fields.", 400, "validation_error", details)
            except ObjectDoesNotExist:
                return _error("Vault item not found.", 404, "not_found")
            response["Cache-Control"] = "no-store"
            response["X-Content-Type-Options"] = "nosniff"
            return response
        return wrapped
    return decorate


@agent_endpoint("GET")
def workspace(request):
    return JsonResponse(workspace_payload(request.vault, request, purpose="agent"))


@agent_endpoint("GET")
def export(request):
    response = HttpResponse(export_markdown(request.vault), content_type="text/markdown; charset=utf-8")
    response["Content-Disposition"] = 'attachment; filename="shvya-vault.md"'
    return response


@agent_endpoint("POST")
def entries(request):
    allowed = {"section", "body", "origin", "source_date", "external_id", "kind", "url", "send_when", "allowed_for_ai_sharing"}
    if set(request.vault_data) - allowed:
        raise ValidationError("Unexpected entry fields.")
    # Binary files are uploaded through the protected client/staff interface.
    kind = request.vault_data.get("kind", "note")
    if not isinstance(kind, str) or kind not in {"note", "link"}:
        raise ValidationError({"kind": "The agent API accepts note or link entries."})
    entry, updated = upsert_entry(request.vault, request.vault_data, author_type="agent")
    return JsonResponse({"id": str(entry.pk), "updated": updated, "entry": entry_payload(entry, request, "agent")}, status=200 if updated else 201)


@agent_endpoint("POST")
def questions(request):
    if set(request.vault_data) - {"section", "text", "external_id"}:
        raise ValidationError("Unexpected question fields.")
    question, updated = upsert_question(request.vault, request.vault_data, author_type="agent")
    return JsonResponse({"id": str(question.pk), "updated": updated, "question": question_payload(question)}, status=200 if updated else 201)


@agent_endpoint("POST")
def calls(request):
    if set(request.vault_data) - {"title", "date", "url", "duration_min", "attendees", "summary", "external_id", "share_recording"}:
        raise ValidationError("Unexpected call fields.")
    call, updated = upsert_call(request.vault, request.vault_data)
    return JsonResponse({"id": str(call.pk), "updated": updated, "call": call_payload(call, include_private_recording=True)}, status=200 if updated else 201)
