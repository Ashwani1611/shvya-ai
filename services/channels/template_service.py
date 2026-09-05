"""Real WhatsApp template lifecycle: drafts, Meta submit/sync/delete/copy and CRM variables."""

import json
import logging
import re
from urllib.parse import urlparse

from django.conf import settings
from django.core.exceptions import ValidationError
from django.db import transaction
from django.utils import timezone

from apps.channels.models import WhatsAppAccount, WhatsAppTemplate
from apps.channels.providers import whatsapp as meta
from apps.channels.providers.whatsapp import WhatsAppAPIError, WhatsAppClient
from apps.channels.template_models import WhatsAppTemplateMetadata, WhatsAppTemplateOperation
from apps.crm.models.attribute import AttributeDefinition

logger = logging.getLogger(__name__)
NAME_RE = re.compile(r"^[a-z0-9_]{1,512}$")
VAR_RE = re.compile(r"\{\{\s*([A-Za-z0-9_]+)\s*\}\}")
META_VAR_RE = re.compile(r"\{\{\s*(\d+)\s*\}\}")
PHONE_RE = re.compile(r"^\+?[0-9][0-9\-\s()]{5,19}$")

MEDIA_RULES = {
    WhatsAppTemplate.AttachmentType.IMAGE: {
        "mime_types": {"image/jpeg", "image/png"},
        "max_bytes": 5 * 1024 * 1024,
        "label": "Image",
    },
    WhatsAppTemplate.AttachmentType.VIDEO: {
        "mime_types": {"video/mp4", "video/3gpp"},
        "max_bytes": 16 * 1024 * 1024,
        "label": "Video",
    },
    WhatsAppTemplate.AttachmentType.DOCUMENT: {
        "mime_types": {
            "application/pdf",
            "application/msword",
            "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
            "application/vnd.ms-excel",
            "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            "application/vnd.ms-powerpoint",
            "application/vnd.openxmlformats-officedocument.presentationml.presentation",
            "text/plain",
        },
        "max_bytes": 100 * 1024 * 1024,
        "label": "Document",
    },
}


class TemplateError(Exception):
    def __init__(self, message, *, status_code=None, meta_error_code=""):
        super().__init__(message)
        self.status_code = status_code
        self.meta_error_code = str(meta_error_code or "")


def state_for(template):
    return WhatsAppTemplateMetadata.objects.get_or_create(template=template)[0]


def available_placeholders(*, organization):
    base = [
        ("lead_name", "Lead name", "lead", "name", "John Smith"),
        ("lead_first_name", "Lead first name", "lead", "name", "John"),
        ("phone", "Phone", "lead", "phone", "+919876543210"),
        ("email", "Email", "lead", "email", "lead@example.com"),
        ("lead_source", "Lead source", "lead", "lead_source", "Website"),
        ("pipeline_name", "Pipeline", "lead", "pipeline", "Sales"),
        ("stage_name", "Stage", "lead", "stage", "Qualified"),
        ("org_name", "Organization name", "organization", "name", organization.name),
        ("org_id", "Organization ID", "organization", "id", str(organization.id)),
        ("user_name", "User name", "user", "name", "Team member"),
    ]
    result = [{"key": k, "label": l, "source": s, "field_name": f, "data_type": "text", "description": l, "supported": True, "example": e} for k, l, s, f, e in base]
    for item in AttributeDefinition.objects.filter(organization=organization):
        result.append({
            "key": item.key,
            "label": item.name,
            "source": "lead_attribute",
            "field_name": item.key,
            "data_type": item.field_type,
            "description": item.description or item.name,
            "supported": True,
            "example": item.options[0] if item.options else f"Example {item.name}",
        })
    return result


def build_meta_body(*, organization, body):
    allowed = {p["key"] for p in available_placeholders(organization=organization)}
    mapping, reverse = {}, {}

    def repl(match):
        key = match.group(1)
        if key not in allowed:
            raise TemplateError(f"Unknown template placeholder: {key}.")
        if key not in reverse:
            number = str(len(reverse) + 1)
            reverse[key], mapping[number] = number, key
        return "{{" + reverse[key] + "}}"

    return VAR_RE.sub(repl, body), mapping


def _error(exc):
    try:
        payload = json.loads(exc.response_body or "{}")
    except (TypeError, ValueError):
        payload = {}
    error = payload.get("error") or {}
    details = error.get("error_data") or {}
    return {
        "http_status": exc.status_code,
        "code": str(error.get("code") or ""),
        "subcode": str(error.get("error_subcode") or ""),
        "type": str(error.get("type") or ""),
        "message": str(details.get("details") or error.get("message") or str(exc)),
        "payload": payload,
    }


def _audit(template, operation, success, *, error=None, payload=None):
    err = error or {}
    WhatsAppTemplateOperation.objects.create(
        organization=template.organization,
        template=template,
        account=template.account,
        operation=operation,
        success=success,
        http_status=err.get("http_status"),
        meta_error_code=err.get("code", ""),
        meta_error_subcode=err.get("subcode", ""),
        meta_error_type=err.get("type", ""),
        meta_error_message=err.get("message", ""),
        response_payload=payload or err.get("payload") or {},
    )


def _validate_button_set(buttons):
    buttons = buttons or []
    if len(buttons) > 10:
        raise TemplateError("WhatsApp templates support a maximum of 10 buttons in total.")

    counts = {
        "visit_website": 0,
        "call_phone": 0,
        "copy_offer": 0,
        "text_back": 0,
        "request_contact_info": 0,
    }
    for item in buttons:
        kind = item.get("type")
        if kind not in counts:
            raise TemplateError("Unsupported template button.")
        counts[kind] += 1

    # Keep the common Meta CTA layout deterministic and review-friendly.
    if counts["visit_website"] > 1:
        raise TemplateError("A standard template can contain only one Visit Website button.")
    if counts["call_phone"] > 1:
        raise TemplateError("A standard template can contain only one Call Phone button.")
    if counts["visit_website"] + counts["call_phone"] > 2:
        raise TemplateError("A standard template supports at most two call-to-action buttons.")
    if counts["copy_offer"] > 1:
        raise TemplateError("A standard template can contain only one Copy Code button.")
    if counts["request_contact_info"]:
        raise TemplateError("Request Contact Info is a chat action, not a Meta message-template button.")
    return counts


def validate_template(*, template, for_submit=False):
    template.name = (template.name or "").strip().lower().replace(" ", "_")
    if not NAME_RE.fullmatch(template.name):
        raise TemplateError("Template name may contain only lowercase letters, numbers and underscores.")
    if not (template.body or "").strip():
        raise TemplateError("Message body is required.")
    if len(template.body) > 1024:
        raise TemplateError("Message body cannot exceed 1024 characters.")
    if len(template.footer or "") > 60:
        raise TemplateError("Footer cannot exceed 60 characters.")
    if template.account.organization_id != template.organization_id:
        raise TemplateError("Selected business does not belong to this organization.")
    build_meta_body(organization=template.organization, body=template.body)
    _validate_button_set(template.buttons)
    if for_submit:
        account = template.account
        if account.status != WhatsAppAccount.Status.CONNECTED or not account.is_active:
            raise TemplateError("Selected WhatsApp business is not connected.")
        if not account.waba_id or not account.access_token:
            raise TemplateError("Selected business is missing WABA credentials.")
        if template.template_format == WhatsAppTemplate.Format.CAROUSEL:
            raise TemplateError("Carousel needs real card media/components before Meta submission; SHVYA will not return a fake success.")
    try:
        template.full_clean(exclude=["status"])
    except ValidationError as exc:
        raise TemplateError(str(exc)) from exc


def _button(item):
    kind, text = item.get("type"), (item.get("text") or "").strip()
    if kind == "visit_website":
        url = (item.get("url") or "").strip()
        parsed = urlparse(url)
        if not text or parsed.scheme not in {"http", "https"} or not parsed.netloc:
            raise TemplateError("Visit Website requires button text and a valid URL.")
        return {"type": "URL", "text": text[:25], "url": url}
    if kind == "call_phone":
        phone = (item.get("phone_number") or "").strip()
        if not text or not PHONE_RE.fullmatch(phone):
            raise TemplateError("Call Phone requires button text and a valid international phone number.")
        return {"type": "PHONE_NUMBER", "text": text[:25], "phone_number": phone}
    if kind == "copy_offer":
        code = (item.get("coupon_code") or "").strip()
        if not code:
            raise TemplateError("Copy Code requires a code value.")
        return {"type": "COPY_CODE", "example": code[:15]}
    if kind == "text_back":
        if not text:
            raise TemplateError("Quick Reply requires button text.")
        return {"type": "QUICK_REPLY", "text": text[:25]}
    if kind == "request_contact_info":
        raise TemplateError("Request Contact Info is a chat action, not a standard Meta template button.")
    raise TemplateError("Unsupported template button.")


def _ordered_buttons(buttons):
    """Keep Meta button groups contiguous: CTAs/copy first, quick replies last."""
    cta = [x for x in buttons if x.get("type") in {"visit_website", "call_phone", "copy_offer"}]
    quick = [x for x in buttons if x.get("type") == "text_back"]
    return cta + quick


def _validate_media_file(attachment_type, uploaded_file):
    rule = MEDIA_RULES.get(attachment_type)
    if not rule:
        raise TemplateError("Unsupported media attachment type.")
    if uploaded_file is None:
        raise TemplateError(f"Choose a {rule['label'].lower()} sample file before submitting this template.")
    mime = (getattr(uploaded_file, "content_type", "") or "").lower()
    size = int(getattr(uploaded_file, "size", 0) or 0)
    if mime not in rule["mime_types"]:
        allowed = ", ".join(sorted(rule["mime_types"]))
        raise TemplateError(f"Unsupported {rule['label'].lower()} type. Allowed: {allowed}.")
    if size <= 0:
        raise TemplateError("The selected media file is empty.")
    if size > rule["max_bytes"]:
        max_mb = rule["max_bytes"] // (1024 * 1024)
        raise TemplateError(f"{rule['label']} is too large. Maximum size is {max_mb} MB.")
    return mime, size


def _upload_header_sample(*, account, attachment_type, uploaded_file):
    """Upload a media-header sample to Meta and return its reusable handle."""
    mime, size = _validate_media_file(attachment_type, uploaded_file)
    app_id = str(getattr(settings, "META_APP_ID", "") or "").strip()
    if not app_id:
        raise TemplateError("META_APP_ID is not configured; media template samples cannot be uploaded.")

    headers = {"Authorization": f"Bearer {account.access_token}"}
    session_url = f"{meta.GRAPH_API_BASE}/{app_id}/uploads"
    try:
        response = meta.requests.post(
            session_url,
            headers=headers,
            params={
                "file_length": size,
                "file_type": mime,
                "file_name": getattr(uploaded_file, "name", "template-media"),
            },
            timeout=meta.REQUEST_TIMEOUT_SECONDS,
        )
    except meta.requests.RequestException as exc:
        raise TemplateError(f"Network error creating Meta media upload session: {exc}") from exc
    if not response.ok:
        err = _error(WhatsAppAPIError("Meta media upload session failed.", response.status_code, response.text))
        raise TemplateError(err["message"], status_code=err["http_status"], meta_error_code=err["code"])
    try:
        upload_id = str(response.json().get("id") or "")
    except ValueError as exc:
        raise TemplateError("Meta media upload session returned invalid JSON.") from exc
    if not upload_id:
        raise TemplateError("Meta media upload session did not return an upload ID.")

    try:
        uploaded_file.seek(0)
        upload_response = meta.requests.post(
            f"{meta.GRAPH_API_BASE}/{upload_id}",
            headers={
                **headers,
                "Content-Type": mime,
                "file_offset": "0",
            },
            data=uploaded_file.read(),
            timeout=meta.REQUEST_TIMEOUT_SECONDS,
        )
    except meta.requests.RequestException as exc:
        raise TemplateError(f"Network error uploading template media to Meta: {exc}") from exc
    if not upload_response.ok:
        err = _error(WhatsAppAPIError("Meta media upload failed.", upload_response.status_code, upload_response.text))
        raise TemplateError(err["message"], status_code=err["http_status"], meta_error_code=err["code"])
    try:
        handle = str(upload_response.json().get("h") or "")
    except ValueError as exc:
        raise TemplateError("Meta media upload returned invalid JSON.") from exc
    if not handle:
        raise TemplateError("Meta media upload did not return a header sample handle.")
    return handle, mime, size


def build_meta_payload(*, template, header_handle=""):
    st = state_for(template)
    body, mapping = build_meta_body(organization=template.organization, body=template.body)
    components = []
    if template.attachment_type != WhatsAppTemplate.AttachmentType.NONE:
        if not header_handle:
            raise TemplateError("A Meta header sample handle is required for media templates.")
        components.append({
            "type": "HEADER",
            "format": template.attachment_type.upper(),
            "example": {"header_handle": [header_handle]},
        })
    components.append({"type": "BODY", "text": body})
    if template.footer:
        components.append({"type": "FOOTER", "text": template.footer})
    if template.buttons:
        _validate_button_set(template.buttons)
        buttons = [_button(x) for x in _ordered_buttons(template.buttons)]
        components.append({"type": "BUTTONS", "buttons": buttons})
    st.placeholder_mapping, st.components = mapping, components
    st.save(update_fields=["placeholder_mapping", "components", "updated_at"])
    return {"name": template.name, "language": st.language or "en_US", "category": template.category.upper(), "components": components}


def create_template(*, organization, account, created_by, name, body, category=WhatsAppTemplate.Category.MARKETING, template_format=WhatsAppTemplate.Format.STANDARD, footer="", attachment_type=WhatsAppTemplate.AttachmentType.NONE, buttons=None, language="en_US"):
    template = WhatsAppTemplate(organization=organization, account=account, created_by=created_by, name=name, body=body, category=category, template_format=template_format, footer=footer, attachment_type=attachment_type, buttons=buttons or [], status=WhatsAppTemplate.Status.DRAFT)
    validate_template(template=template)
    _, mapping = build_meta_body(organization=organization, body=body)
    with transaction.atomic():
        template.save()
        WhatsAppTemplateMetadata.objects.create(template=template, language=language or "en_US", placeholder_mapping=mapping)
    return template


def update_draft(*, template, account, name, body, category, template_format, footer="", attachment_type="none", buttons=None, language="en_US"):
    if template.status != WhatsAppTemplate.Status.DRAFT or template.meta_template_id:
        raise TemplateError("Submitted templates cannot be edited in place. Copy to a draft instead.")
    old_attachment_type = template.attachment_type
    template.account, template.name, template.body = account, name, body
    template.category, template.template_format = category, template_format
    template.footer, template.attachment_type, template.buttons = footer, attachment_type, buttons or []
    validate_template(template=template)
    _, mapping = build_meta_body(organization=template.organization, body=body)
    with transaction.atomic():
        template.save()
        st = state_for(template)
        st.language, st.placeholder_mapping, st.local_status = language or "en_US", mapping, WhatsAppTemplateMetadata.LocalStatus.DRAFT
        if old_attachment_type != attachment_type or attachment_type == WhatsAppTemplate.AttachmentType.NONE:
            st.header_sample_handle = ""
            st.header_file_name = ""
            st.header_mime_type = ""
            st.header_file_size = None
        st.save()
    return template


def submit_template(*, template, attachment_file=None):
    if template.status != WhatsAppTemplate.Status.DRAFT:
        raise TemplateError(f"Template is already {template.get_status_display()}.")
    validate_template(template=template, for_submit=True)
    st = state_for(template)
    header_handle = ""
    if template.attachment_type != WhatsAppTemplate.AttachmentType.NONE:
        if attachment_file is not None:
            handle, mime, size = _upload_header_sample(
                account=template.account,
                attachment_type=template.attachment_type,
                uploaded_file=attachment_file,
            )
            st.header_sample_handle = handle
            st.header_file_name = getattr(attachment_file, "name", "")[:255]
            st.header_mime_type = mime
            st.header_file_size = size
            st.save(update_fields=[
                "header_sample_handle",
                "header_file_name",
                "header_mime_type",
                "header_file_size",
                "updated_at",
            ])
        header_handle = st.header_sample_handle
        if not header_handle:
            rule = MEDIA_RULES[template.attachment_type]
            raise TemplateError(f"Choose a {rule['label'].lower()} sample file before submitting this template.")

    payload = build_meta_payload(template=template, header_handle=header_handle)
    st.local_status = WhatsAppTemplateMetadata.LocalStatus.SUBMITTING
    st.save(update_fields=["local_status", "updated_at"])
    try:
        client = WhatsAppClient(template.account.phone_number_id, template.account.access_token)
        response = client._post(f"{template.account.waba_id}/message_templates", payload)
    except WhatsAppAPIError as exc:
        err = _error(exc)
        st.local_status = WhatsAppTemplateMetadata.LocalStatus.SYNC_ERROR
        st.meta_error_code, st.meta_error_subcode = err["code"], err["subcode"]
        st.meta_error_type, st.meta_error_message, st.meta_response = err["type"], err["message"], err["payload"]
        st.save()
        template.rejection_reason = err["message"]
        template.save(update_fields=["rejection_reason", "updated_at"])
        _audit(template, WhatsAppTemplateOperation.Operation.SUBMIT, False, error=err)
        raise TemplateError(err["message"], status_code=err["http_status"], meta_error_code=err["code"]) from exc
    template.meta_template_id = str(response.get("id") or "")
    template.status = normalize_meta_status(response.get("status") or "PENDING")
    template.rejection_reason = ""
    template.save(update_fields=["meta_template_id", "status", "rejection_reason", "updated_at"])
    st.local_status, st.submitted_at, st.meta_response = WhatsAppTemplateMetadata.LocalStatus.SUBMITTED, timezone.now(), response
    st.meta_error_code = st.meta_error_subcode = st.meta_error_type = st.meta_error_message = ""
    st.save()
    _audit(template, WhatsAppTemplateOperation.Operation.SUBMIT, True, payload=response)
    return template


def normalize_meta_status(value):
    value = (value or "").strip().lower().replace(" ", "_")
    if value == "disabled":
        return WhatsAppTemplate.Status.PAUSED
    allowed = {v for v, _ in WhatsAppTemplate.Status.choices}
    return value if value in allowed else WhatsAppTemplate.Status.PENDING


def _remote_templates(account):
    if not account.waba_id or not account.access_token:
        raise TemplateError("Selected business is missing WABA credentials.")
    url = f"{meta.GRAPH_API_BASE}/{account.waba_id}/message_templates"
    params = {"fields": "id,name,status,category,language,components,rejected_reason", "limit": 100}
    headers = {"Authorization": f"Bearer {account.access_token}"}
    rows = []
    while url:
        try:
            response = meta.requests.get(url, headers=headers, params=params, timeout=meta.REQUEST_TIMEOUT_SECONDS)
        except meta.requests.RequestException as exc:
            raise WhatsAppAPIError(f"Network error listing templates: {exc}") from exc
        if not response.ok:
            raise WhatsAppAPIError("WhatsApp template listing failed.", response.status_code, response.text)
        try:
            data = response.json()
        except ValueError as exc:
            raise WhatsAppAPIError("WhatsApp template listing returned invalid JSON.", response.status_code, response.text) from exc
        rows.extend(data.get("data") or [])
        url, params = (data.get("paging") or {}).get("next"), None
    return rows


def _component_text(components, kind):
    for item in components or []:
        if str(item.get("type", "")).upper() == kind:
            return item.get("text") or ""
    return ""


def sync_templates(*, organization, account):
    if account.organization_id != organization.id:
        raise TemplateError("Selected business does not belong to this organization.")
    try:
        remote = _remote_templates(account)
    except WhatsAppAPIError as exc:
        err = _error(exc)
        raise TemplateError(err["message"], status_code=err["http_status"], meta_error_code=err["code"]) from exc
    created = updated = unchanged = 0
    seen, now = set(), timezone.now()
    with transaction.atomic():
        for item in remote:
            meta_id, name = str(item.get("id") or ""), (item.get("name") or "").strip()
            if not name:
                continue
            if meta_id:
                seen.add(meta_id)
            template = WhatsAppTemplate.objects.filter(organization=organization, account=account, meta_template_id=meta_id).first() if meta_id else None
            template = template or WhatsAppTemplate.objects.filter(organization=organization, account=account, name=name).first()
            components = item.get("components") or []
            if template is None:
                template = WhatsAppTemplate.objects.create(organization=organization, account=account, name=name, category=str(item.get("category") or "MARKETING").lower(), status=normalize_meta_status(item.get("status")), body=_component_text(components, "BODY"), footer=_component_text(components, "FOOTER")[:60], meta_template_id=meta_id, rejection_reason=item.get("rejected_reason") or "")
                WhatsAppTemplateMetadata.objects.create(template=template, local_status=WhatsAppTemplateMetadata.LocalStatus.SYNCED, language=item.get("language") or "en_US", components=components, meta_response=item, last_synced_at=now)
                created += 1
            else:
                before = (template.status, template.category, template.rejection_reason, template.meta_template_id)
                template.meta_template_id = meta_id or template.meta_template_id
                template.status = normalize_meta_status(item.get("status"))
                template.category = str(item.get("category") or template.category).lower()
                template.rejection_reason = item.get("rejected_reason") or ""
                template.save(update_fields=["meta_template_id", "status", "category", "rejection_reason", "updated_at"])
                st = state_for(template)
                st.local_status, st.language, st.components, st.meta_response, st.last_synced_at = WhatsAppTemplateMetadata.LocalStatus.SYNCED, item.get("language") or st.language, components, item, now
                if template.status == WhatsAppTemplate.Status.APPROVED and not st.approved_at:
                    st.approved_at = now
                if template.status == WhatsAppTemplate.Status.REJECTED and not st.rejected_at:
                    st.rejected_at = now
                st.save()
                after = (template.status, template.category, template.rejection_reason, template.meta_template_id)
                updated += before != after
                unchanged += before == after
        for template in WhatsAppTemplate.objects.filter(organization=organization, account=account).exclude(meta_template_id="").exclude(status=WhatsAppTemplate.Status.DRAFT):
            if template.meta_template_id not in seen:
                st = state_for(template)
                st.local_status, st.last_synced_at = WhatsAppTemplateMetadata.LocalStatus.REMOTE_DELETED, now
                st.save(update_fields=["local_status", "last_synced_at", "updated_at"])
    return {"created": created, "updated": updated, "unchanged": unchanged, "failed": 0, "total": len(remote)}


def copy_template(*, template, created_by):
    base, name, n = f"{template.name}_copy", f"{template.name}_copy", 2
    while WhatsAppTemplate.objects.filter(account=template.account, name=name).exists():
        name, n = f"{base}_{n}", n + 1
    source = state_for(template)
    with transaction.atomic():
        copied = WhatsAppTemplate.objects.create(organization=template.organization, account=template.account, name=name, category=template.category, template_format=template.template_format, status=WhatsAppTemplate.Status.DRAFT, body=template.body, footer=template.footer, attachment_type=template.attachment_type, buttons=template.buttons, created_by=created_by)
        WhatsAppTemplateMetadata.objects.create(template=copied, language=source.language, placeholder_mapping=source.placeholder_mapping)
        _audit(copied, WhatsAppTemplateOperation.Operation.COPY, True, payload={"copied_from": str(template.id)})
    return copied


def delete_template(*, template):
    if not template.meta_template_id:
        template.delete()
        return
    account = template.account
    url = f"{meta.GRAPH_API_BASE}/{account.waba_id}/message_templates"
    try:
        response = meta.requests.delete(url, headers={"Authorization": f"Bearer {account.access_token}"}, params={"name": template.name, "hsm_id": template.meta_template_id}, timeout=meta.REQUEST_TIMEOUT_SECONDS)
    except meta.requests.RequestException as exc:
        raise TemplateError(f"Network error deleting WhatsApp template: {exc}") from exc
    if not response.ok and response.status_code != 404:
        err = _error(WhatsAppAPIError("WhatsApp template deletion failed.", response.status_code, response.text))
        _audit(template, WhatsAppTemplateOperation.Operation.DELETE, False, error=err)
        raise TemplateError(err["message"], status_code=err["http_status"], meta_error_code=err["code"])
    try:
        payload = response.json()
    except ValueError:
        payload = {"deleted": True}
    _audit(template, WhatsAppTemplateOperation.Operation.DELETE, True, payload=payload)
    template.status = WhatsAppTemplate.Status.PENDING_DELETION
    template.save(update_fields=["status", "updated_at"])
    st = state_for(template)
    st.local_status, st.deleted_at, st.meta_response = WhatsAppTemplateMetadata.LocalStatus.DELETED, timezone.now(), payload
    st.save()


def render_template_body(*, template, lead, user=None):
    values = {"lead_name": lead.name, "lead_first_name": (lead.name or "").split(" ")[0], "phone": lead.phone, "email": lead.email or "", "lead_source": getattr(lead, "lead_source", "") or "", "org_name": lead.organization.name, "org_id": str(lead.organization_id), "user_name": getattr(user, "name", "") or getattr(user, "email", "") or "", "pipeline_name": lead.pipeline.name if lead.pipeline_id else "", "stage_name": lead.stage.name if lead.stage_id else ""}
    values.update(getattr(lead, "attributes", None) or {})
    return VAR_RE.sub(lambda m: str(values.get(m.group(1), m.group(0))), template.body)


# Backward-compatible name used by older views/templates.
AVAILABLE_VARIABLES = []
