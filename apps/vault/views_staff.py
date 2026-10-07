"""SHVYA superadmin Vault console; uses the dedicated area session."""
from django.contrib import messages
from django.contrib.auth.decorators import user_passes_test
from django.core.exceptions import ValidationError
from django.core.paginator import Paginator
from django.db import IntegrityError, transaction
from django.db.models import Count, Q
from django.http import FileResponse, Http404, HttpResponse, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.views.decorators.http import require_GET, require_http_methods

from apps.organizations.models import Organization
from apps.superadmin.models import AuditLog
from . import services
from .models import Vault, VaultEvent, VaultProfileSnapshot, VaultQuestion
from .views_common import apply_workspace_action, export_filename, private_response, workspace_context


superadmin_required = user_passes_test(
    lambda user: user.is_authenticated and user.is_active and user.is_superuser,
    login_url="/superadmin/login/",
)


def audit(request, vault, operation):
    AuditLog.record(actor=request.user, action=AuditLog.Action.ORGANIZATION_UPDATED,
                    target=vault.organization, request=request, operation=f"vault_{operation}", vault_id=str(vault.id))


@superadmin_required
@require_http_methods(["GET", "POST"])
def vault_list(request):
    if request.method == "POST":
        action = request.POST.get("action", "")
        try:
            with transaction.atomic():
                if action == "create":
                    org = get_object_or_404(Organization, id=request.POST.get("organization_id"))
                    vault, code = services.create_vault(org, name=request.POST.get("name") or None)
                    audit(request, vault, "created")
                    return _workspace(request, vault, fresh_secret={"label": "Client access code", "value": code,
                        "help": "Shown once. Share this code and the private link with this client. You can generate a new code at any time."})
                vault = get_object_or_404(Vault.objects.select_for_update(), id=request.POST.get("vault_id"))
                secret = None
                if action in {"pause", "resume"}:
                    vault.is_paused = action == "pause"
                    if vault.is_paused:
                        vault.access_version += 1
                    vault.save(update_fields=["is_paused", "access_version", "updated_at"])
                elif action == "rotate_code":
                    secret = {"label": "New client access code", "value": services.rotate_access_code(vault),
                              "help": "Previous client sessions and the old code are now invalid. Shown once."}
                elif action == "rotate_token":
                    secret = {"label": "Vault agent token", "value": services.rotate_agent_token(vault),
                              "help": "Shown once. Valid for 90 days for this Vault only. The previous token is revoked."}
                elif action == "revoke_token":
                    vault.token_hash = None
                    vault.token_prefix = ""
                    vault.token_expires_at = None
                    vault.save(update_fields=["token_hash", "token_prefix", "token_expires_at", "updated_at"])
                elif action == "quota":
                    quota_mb = int(request.POST.get("storage_quota_mb", "0"))
                    if quota_mb not in {512, 1024, 2048, 5120, 10240}:
                        raise ValidationError("Choose a storage allowance from the list.")
                    quota = quota_mb * 1024 * 1024
                    if quota < vault.storage_used_bytes:
                        raise ValidationError("The storage allowance cannot be lower than the files already stored.")
                    vault.storage_quota_bytes = quota
                    vault.save(update_fields=["storage_quota_bytes", "updated_at"])
                else:
                    raise ValidationError("Choose a valid Vault action.")
                audit(request, vault, action)
                if secret:
                    vault.refresh_from_db()
                    return _workspace(request, vault, fresh_secret=secret)
            messages.success(request, "Vault settings updated.")
            return redirect("vault-staff-list")
        except (ValidationError, ValueError, IntegrityError) as exc:
            text = "This organization already has a Vault." if isinstance(exc, IntegrityError) else _error(exc)
            messages.error(request, text)

    q = request.GET.get("q", "").strip()[:200]
    status = request.GET.get("status", "")
    vaults = Vault.objects.select_related("organization").prefetch_related("sections").annotate(
        item_count=Count("entries", distinct=True), file_count=Count("entries", filter=Q(entries__kind__in=["file", "audio"]), distinct=True),
        open_questions=Count("questions", filter=Q(questions__answered_at__isnull=True), distinct=True),
    )
    if q:
        vaults = vaults.filter(Q(name__icontains=q) | Q(organization__name__icontains=q))
    if status in {"draft", "submitted"}:
        vaults = vaults.filter(status=status)
    elif status == "paused":
        vaults = vaults.filter(is_paused=True)
    page = Paginator(vaults.order_by("-updated_at", "id"), 30).get_page(request.GET.get("page"))
    rows = []
    for vault in page:
        states = list(vault.sections.all())
        done = sum(s.is_done or s.state == "dont_have" for s in states)
        rows.append({"vault": vault, "detail_url": reverse("vault-staff-detail", kwargs={"vault_id": vault.id}),
                     "private_url": request.build_absolute_uri(reverse("vault-client", kwargs={"slug": vault.slug})),
                     "action_url": reverse("vault-staff-list"), "progress": round(done * 100 / 15),
                     "filled_count": done, "open_questions": vault.open_questions,
                     "item_count": vault.item_count, "file_count": vault.file_count,
                     "storage_display": _bytes(vault.storage_used_bytes), "quota_display": _bytes(vault.storage_quota_bytes),
                     "quota_mb": vault.storage_quota_bytes // (1024 * 1024),
                     "storage_percent": min(100, round(vault.storage_used_bytes * 100 / max(1, vault.storage_quota_bytes)))})
    return private_response(render(request, "vault/list.html", {
        "vault_rows": rows, "page_obj": page,
        "organizations": Organization.objects.filter(vault__isnull=True).order_by("name").only("id", "name"),
        "organisations": Organization.objects.filter(vault__isnull=True).order_by("name").only("id", "name"),
        "stats": {"total": Vault.objects.count(), "active": Vault.objects.filter(is_paused=False).count(),
                  "submitted": Vault.objects.filter(status="submitted").count(),
                  "open_questions": VaultQuestion.objects.filter(answered_at__isnull=True).count()},
        "q": q, "status": status, "action_url": reverse("vault-staff-list"),
        "list_url": reverse("vault-staff-list"), "create_url": reverse("vault-staff-list"),
        "events": VaultEvent.objects.filter(kind__in=["updated", "submitted"]).select_related("vault").order_by("-created_at")[:8],
    }))


def _workspace(request, vault, **extra):
    context = workspace_context(request, vault, staff=True)
    context["action_url"] = reverse("vault-staff-detail", kwargs={"vault_id": vault.id})
    context["settings_action_url"] = reverse("vault-staff-list")
    context.update(extra)
    return private_response(render(request, "vault/workspace.html", context))


@superadmin_required
@require_http_methods(["GET", "POST"])
def vault_detail(request, vault_id):
    vault = get_object_or_404(Vault.objects.select_related("organization"), pk=vault_id)
    if request.method == "POST":
        try:
            with transaction.atomic():
                vault = Vault.objects.select_for_update().get(pk=vault.pk)
                message = apply_workspace_action(request, vault, staff=True)
                audit(request, vault, request.POST.get("action", "updated"))
            messages.success(request, message)
            response = redirect("vault-staff-detail", vault_id=vault.id)
            from .sections import SECTION_KEYS
            section = request.POST.get("section", "")
            if section in SECTION_KEYS:
                response["Location"] += f"#{section}"
            elif request.POST.get("action") == "add_call":
                response["Location"] += "#calls"
            elif request.POST.get("action") == "profile_snapshot":
                response["Location"] += "#review"
            return private_response(response)
        except (ValidationError, ValueError) as exc:
            messages.error(request, _error(exc))
            vault.refresh_from_db()
            return _workspace(request, vault, form_error=_error(exc))
    return _workspace(request, vault)


@superadmin_required
@require_GET
def vault_export(request, vault_id):
    vault = get_object_or_404(Vault, pk=vault_id)
    response = HttpResponse(services.export_markdown(vault), content_type="text/markdown; charset=utf-8")
    response["Content-Disposition"] = f'attachment; filename="{export_filename(vault)}"'
    return private_response(response)


@superadmin_required
@require_GET
def vault_file(request, vault_id, entry_id):
    vault = get_object_or_404(Vault, pk=vault_id)
    entry = get_object_or_404(vault.entries, pk=entry_id)
    return serve_file(entry)


def serve_file(entry):
    if not entry.file:
        raise Http404("No file is attached.")
    try:
        response = FileResponse(entry.file.open("rb"), as_attachment=True, filename=entry.file_name,
                                content_type="application/octet-stream")
    except FileNotFoundError as exc:
        raise Http404("This file is unavailable.") from exc
    return private_response(response)


@superadmin_required
@require_GET
def vault_profile(request, vault_id, snapshot_id):
    snapshot = get_object_or_404(VaultProfileSnapshot.objects.select_related("vault"), vault_id=vault_id, pk=snapshot_id)
    response = JsonResponse(snapshot.body, json_dumps_params={"ensure_ascii": False, "indent": 2})
    response["Content-Disposition"] = f'attachment; filename="{export_filename(snapshot.vault, "json")}"'
    return private_response(response)


def _error(exc):
    return " ".join(exc.messages) if isinstance(exc, ValidationError) else "Please check the entered values."


def _bytes(value):
    if value >= 1024 ** 3:
        return f"{value / 1024 ** 3:.1f} GB"
    return f"{value / 1024 ** 2:.1f} MB"
