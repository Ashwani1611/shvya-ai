"""Tenant-safe UI endpoints for WhatsApp Message Template management."""

import json

from django.contrib import messages
from django.db.models import Q
from django.http import JsonResponse
from django.shortcuts import redirect, render
from django.views.decorators.http import require_GET, require_POST

from apps.crm.decorators import crm_login_required
from services.channels.template_service import (
    TemplateError,
    available_placeholders,
    copy_template,
    create_template,
    delete_template,
    state_for,
    submit_template,
    sync_templates,
    update_draft,
)

from . import views_flat
from .models import WhatsAppAccount, WhatsAppTemplate
from .template_models import WhatsAppTemplateMetadata


def _admin(user):
    return views_flat._admin_required(user)


def _accounts(user):
    return WhatsAppAccount.objects.filter(
        organization=user.organization,
        status=WhatsAppAccount.Status.CONNECTED,
        is_active=True,
    ).order_by("business_name", "display_phone_number")


def _account(user, value):
    if not value:
        return None
    return _accounts(user).filter(id=value).first()


def _template(user, template_id):
    return (
        WhatsAppTemplate.objects.filter(id=template_id, organization=user.organization)
        .select_related("account")
        .first()
    )


def _buttons(request):
    raw = request.POST.get("buttons_json", "[]")
    try:
        data = json.loads(raw)
    except (TypeError, ValueError):
        raise TemplateError("Button configuration is invalid JSON.")
    if not isinstance(data, list):
        raise TemplateError("Button configuration must be a list.")
    return data


def _form_values(request, template=None):
    return {
        "name": request.POST.get("name", template.name if template else ""),
        "category": request.POST.get("category", template.category if template else WhatsAppTemplate.Category.MARKETING),
        "account": request.POST.get("account", str(template.account_id) if template else ""),
        "template_format": request.POST.get("template_format", template.template_format if template else WhatsAppTemplate.Format.STANDARD),
        "body": request.POST.get("body", template.body if template else ""),
        "footer": request.POST.get("footer", template.footer if template else ""),
        "attachment_type": request.POST.get("attachment_type", template.attachment_type if template else WhatsAppTemplate.AttachmentType.NONE),
        "language": request.POST.get("language", state_for(template).language if template else "en_US"),
        "buttons_json": request.POST.get("buttons_json", json.dumps(template.buttons if template else [])),
    }


@crm_login_required
@require_GET
def template_list(request):
    user = request.crm_user
    qs = WhatsAppTemplate.objects.filter(organization=user.organization).select_related("account")
    category = (request.GET.get("category") or "").strip().lower()
    status = (request.GET.get("status") or "").strip().lower()
    account_id = (request.GET.get("account") or "").strip()
    query = (request.GET.get("q") or "").strip()
    if category:
        qs = qs.filter(category=category)
    if status:
        qs = qs.filter(status=status)
    if account_id:
        qs = qs.filter(account_id=account_id)
    if query:
        qs = qs.filter(Q(name__icontains=query) | Q(rejection_reason__icontains=query))
    # Hide confirmed local deletions; preserve them in DB/audit until Meta sync confirms state.
    qs = qs.exclude(meta_state__local_status=WhatsAppTemplateMetadata.LocalStatus.DELETED)
    return render(request, "channels/whatsapp_template_list.html", {
        "templates": qs,
        "accounts": _accounts(user),
        "categories": [(WhatsAppTemplate.Category.MARKETING, "Marketing"), (WhatsAppTemplate.Category.UTILITY, "Utility")],
        "statuses": WhatsAppTemplate.Status.choices,
        "selected_category": category,
        "selected_status": status,
        "selected_account": account_id,
        "search_query": query,
        "can_manage": _admin(user),
    })


@crm_login_required
def template_create(request):
    user = request.crm_user
    if not _admin(user):
        messages.error(request, "Only organization admins can create message templates.")
        return redirect("whatsapp-template-list")
    values = _form_values(request)
    if request.method == "POST":
        account = _account(user, values["account"])
        if not account:
            messages.error(request, "Select a connected WhatsApp business.")
        else:
            try:
                template = create_template(
                    organization=user.organization,
                    account=account,
                    created_by=user,
                    name=values["name"],
                    body=values["body"],
                    category=values["category"],
                    template_format=values["template_format"],
                    footer=values["footer"],
                    attachment_type=values["attachment_type"],
                    buttons=_buttons(request),
                    language=values["language"],
                )
                if request.POST.get("action") == "submit":
                    submit_template(template=template)
                    messages.success(request, f'Template "{template.name}" submitted to Meta. Current status: {template.get_status_display()}.')
                else:
                    messages.success(request, f'Template "{template.name}" saved as draft.')
                return redirect("whatsapp-template-list")
            except TemplateError as exc:
                messages.error(request, str(exc))
            except Exception as exc:
                messages.error(request, f"Could not save template: {exc}")
    return _render_editor(request, user, values=values, template=None)


@crm_login_required
def template_edit(request, template_id):
    user = request.crm_user
    template = _template(user, template_id)
    if not template:
        messages.error(request, "Template not found.")
        return redirect("whatsapp-template-list")
    if not _admin(user):
        messages.error(request, "Only organization admins can edit message templates.")
        return redirect("whatsapp-template-list")
    if template.status != WhatsAppTemplate.Status.DRAFT or template.meta_template_id:
        messages.warning(request, "Submitted Meta templates are immutable. Copy this template to create an editable draft.")
        return redirect("whatsapp-template-list")
    values = _form_values(request, template)
    if request.method == "POST":
        account = _account(user, values["account"])
        if not account:
            messages.error(request, "Select a connected WhatsApp business.")
        else:
            try:
                update_draft(
                    template=template,
                    account=account,
                    name=values["name"],
                    body=values["body"],
                    category=values["category"],
                    template_format=values["template_format"],
                    footer=values["footer"],
                    attachment_type=values["attachment_type"],
                    buttons=_buttons(request),
                    language=values["language"],
                )
                if request.POST.get("action") == "submit":
                    submit_template(template=template)
                    messages.success(request, f'Template "{template.name}" submitted to Meta.')
                else:
                    messages.success(request, f'Template "{template.name}" updated as draft.')
                return redirect("whatsapp-template-list")
            except TemplateError as exc:
                messages.error(request, str(exc))
    return _render_editor(request, user, values=values, template=template)


def _render_editor(request, user, *, values, template):
    placeholders = available_placeholders(organization=user.organization)
    return render(request, "channels/whatsapp_template_create.html", {
        "template": template,
        "values": values,
        "accounts": _accounts(user),
        "categories": [(WhatsAppTemplate.Category.MARKETING, "Marketing"), (WhatsAppTemplate.Category.UTILITY, "Utility")],
        "formats": WhatsAppTemplate.Format.choices,
        "attachments": WhatsAppTemplate.AttachmentType.choices,
        "placeholders": placeholders,
        "placeholders_json": json.dumps(placeholders),
        "buttons_json": values.get("buttons_json") or "[]",
    })


@crm_login_required
@require_POST
def template_submit(request, template_id):
    user = request.crm_user
    template = _template(user, template_id)
    if not template or not _admin(user):
        return JsonResponse({"error": "Template not found or not permitted."}, status=404)
    try:
        submit_template(template=template)
    except TemplateError as exc:
        return JsonResponse({"error": str(exc), "meta_error_code": exc.meta_error_code}, status=exc.status_code or 400)
    return JsonResponse({"ok": True, "status": template.status, "meta_template_id": template.meta_template_id})


@crm_login_required
@require_POST
def template_sync(request):
    user = request.crm_user
    if not _admin(user):
        return JsonResponse({"error": "Only organization admins can sync templates."}, status=403)
    account = _account(user, request.POST.get("account"))
    if not account:
        return JsonResponse({"error": "Select a connected business before syncing."}, status=400)
    try:
        summary = sync_templates(organization=user.organization, account=account)
    except TemplateError as exc:
        return JsonResponse({"error": str(exc), "meta_error_code": exc.meta_error_code}, status=exc.status_code or 502)
    return JsonResponse({"ok": True, **summary})


@crm_login_required
@require_POST
def template_copy(request, template_id):
    user = request.crm_user
    source = _template(user, template_id)
    if not source or not _admin(user):
        return JsonResponse({"error": "Template not found or not permitted."}, status=404)
    copied = copy_template(template=source, created_by=user)
    return JsonResponse({"ok": True, "id": str(copied.id), "edit_url": f"/dashboard/whatsapp/templates/{copied.id}/edit/"})


@crm_login_required
@require_POST
def template_delete(request, template_id):
    user = request.crm_user
    template = _template(user, template_id)
    if not template or not _admin(user):
        return JsonResponse({"error": "Template not found or not permitted."}, status=404)
    try:
        delete_template(template=template)
    except TemplateError as exc:
        return JsonResponse({"error": str(exc), "meta_error_code": exc.meta_error_code}, status=exc.status_code or 502)
    return JsonResponse({"ok": True})


@crm_login_required
@require_GET
def template_placeholders(request):
    return JsonResponse({"placeholders": available_placeholders(organization=request.crm_user.organization)})
