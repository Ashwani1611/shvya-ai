"""Private client portal. A Vault grant never authenticates a SHVYA account."""
from datetime import timedelta

from django.conf import settings
from django.contrib import messages
from django.core.exceptions import PermissionDenied, ValidationError
from django.db import transaction
from django.http import HttpResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone
from django.views.decorators.http import require_GET, require_http_methods

from . import services
from .models import Vault, VaultEntry
from .views_common import apply_workspace_action, export_filename, private_response, workspace_context
from .views_staff import _error, serve_file

COOKIE_SALT = "shvya.vault.client.v1"


def cookie_name(vault):
    return f"shvya_vault_{vault.id.hex}"


def has_access(request, vault):
    if vault.is_paused:
        return False
    value = request.get_signed_cookie(cookie_name(vault), default="", salt=COOKIE_SALT,
        max_age=getattr(settings, "VAULT_CLIENT_SESSION_AGE", 43200))
    return value == f"{vault.id}:{vault.access_version}"


def _gate(request, vault, *, error="", status=200):
    return private_response(render(request, "vault/gate.html", {
        "vault_name": vault.name, "vault": vault, "error": error,
        "paused": vault.is_paused, "action_url": reverse("vault-client", kwargs={"slug": vault.slug}),
    }, status=status))


@require_http_methods(["GET", "POST"])
def vault_client(request, slug):
    vault = get_object_or_404(Vault, slug=slug)
    if vault.is_paused:
        return _gate(request, vault, status=403)
    if request.method == "POST":
        # Serialize unlock attempts and writes against code rotation/pause.
        with transaction.atomic():
            vault = Vault.objects.select_for_update().get(pk=vault.pk)
            if vault.is_paused:
                return _gate(request, vault, status=403)
            if "code" in request.POST:
                now = timezone.now()
                if vault.access_locked_until and vault.access_locked_until > now:
                    response = _gate(request, vault, error="Too many attempts. Please try again in 15 minutes or contact your SHVYA team.", status=429)
                    response["Retry-After"] = str(max(1, int((vault.access_locked_until - now).total_seconds())))
                    return response
                if vault.access_locked_until:
                    vault.failed_access_attempts = 0
                    vault.access_locked_until = None
                code = request.POST.get("code", "")
                if len(code) != 6 or not code.isascii() or not code.isdigit() or not vault.check_access_code(code):
                    vault.failed_access_attempts += 1
                    if vault.failed_access_attempts >= 8:
                        vault.access_locked_until = now + timedelta(minutes=15)
                    vault.save(update_fields=["failed_access_attempts", "access_locked_until"])
                    return _gate(request, vault, error="The access code is incorrect. Please check it and try again.", status=400)
                vault.failed_access_attempts = 0
                vault.access_locked_until = None
                vault.save(update_fields=["failed_access_attempts", "access_locked_until"])
                response = redirect("vault-client", slug=vault.slug)
                response.set_signed_cookie(cookie_name(vault), f"{vault.id}:{vault.access_version}", salt=COOKIE_SALT,
                    max_age=getattr(settings, "VAULT_CLIENT_SESSION_AGE", 43200), httponly=True,
                    secure=request.is_secure() or getattr(settings, "SESSION_COOKIE_SECURE", False),
                    samesite="Lax", path=reverse("vault-client", kwargs={"slug": vault.slug}))
                return private_response(response)
            if not has_access(request, vault):
                return _gate(request, vault, error="Enter your access code to continue.", status=403)
            if request.POST.get("action") == "logout":
                response = redirect("vault-client", slug=vault.slug)
                response.delete_cookie(cookie_name(vault), path=reverse("vault-client", kwargs={"slug": vault.slug}), samesite="Lax")
                return private_response(response)
            try:
                # Savepoint ensures invalid forms never leave partial writes.
                with transaction.atomic():
                    message = apply_workspace_action(request, vault)
                messages.success(request, message)
                response = redirect("vault-client", slug=vault.slug)
                section = request.POST.get("section", "")
                from .sections import SECTION_KEYS
                if section in SECTION_KEYS:
                    response["Location"] += f"#{section}"
                elif request.POST.get("action") == "submit":
                    response["Location"] += "#review"
                return private_response(response)
            except (ValidationError, ValueError) as exc:
                messages.error(request, _error(exc))
                vault.refresh_from_db()
                context = workspace_context(request, vault)
                context["form_error"] = _error(exc)
                return private_response(render(request, "vault/workspace.html", context, status=400))
    if not has_access(request, vault):
        return _gate(request, vault)
    return private_response(render(request, "vault/workspace.html", workspace_context(request, vault)))


@require_GET
def vault_export(request, slug):
    vault = get_object_or_404(Vault, slug=slug)
    if not has_access(request, vault):
        raise PermissionDenied("Unlock this Vault before downloading it.")
    response = HttpResponse(services.export_markdown(vault), content_type="text/markdown; charset=utf-8")
    response["Content-Disposition"] = f'attachment; filename="{export_filename(vault)}"'
    return private_response(response)


@require_GET
def vault_file(request, slug, entry_id):
    vault = get_object_or_404(Vault, slug=slug)
    if not has_access(request, vault):
        raise PermissionDenied("Unlock this Vault before downloading a file.")
    entry = get_object_or_404(vault.entries, pk=entry_id)
    return serve_file(entry)


@require_GET
def signed_file(request, entry_id):
    entry = get_object_or_404(VaultEntry.objects.select_related("vault"), pk=entry_id)
    if not services.verify_download_signature(entry, request.GET.get("signature", "")):
        raise PermissionDenied("This file link has expired or was revoked. Pull the Vault again.")
    return serve_file(entry)
