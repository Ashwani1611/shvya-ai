"""WhatsApp template lifecycle: drafts, Meta submit/sync/delete/copy and CRM variables."""

import copy
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
CARD_UID_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")

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
CAROUSEL_MEDIA_TYPES = {
    WhatsAppTemplate.AttachmentType.IMAGE,
    WhatsAppTemplate.AttachmentType.VIDEO,
}
CAROUSEL_BUTTON_TYPES = {"text_back", "visit_website", "call_phone"}


class TemplateError(Exception):
    def __init__(self, message, *, status_code=None, meta_error_code=""):
        super().__init__(message)
        self.status_code = status_code
        self.meta_error_code = str(meta_error_code or "")


def state_for(template):
    return WhatsAppTemplateMetadata.objects.get_or_create(template=template)[0]


def available_placeholders(*, organization):
    """Return tenant-safe CRM placeholders exposed to template authors."""
    base = [
        ("lead_name", "Lead name", "lead", "name", "John Smith"),
        ("lead_first_name", "Lead first name", "lead", "name", "John"),
        ("phone", "Phone", "lead", "phone", "+919876543210"),
        ("email", "Email", "lead", "email", "lead@example.com"),
        ("lead_source", "Lead source", "lead", "lead_source", "Website"),
        ("pipeline_name", "Pipeline", "lead", "pipeline", "Sales"),
        ("stage_name", "Stage", "lead", "stage", "Qualified"),
        ("org_name", "Organization name", "organization", "name", organization.name),
        ("user_name", "User name", "user", "name", "Team member"),
    ]
    result = [
        {
            "key": key,
            "label": label,
            "source": source,
            "field_name": field_name,
            "data_type": "text",
            "description": label,
            "supported": True,
            "example": example,
        }
        for key, label, source, field_name, example in base
    ]
    for item in AttributeDefinition.objects.filter(organization=organization):
        result.append(
            {
                "key": item.key,
                "label": item.name,
                "source": "lead_attribute",
                "field_name": item.key,
                "data_type": item.field_type,
                "description": item.description or item.name,
                "supported": True,
                "example": item.options[0] if item.options else f"Example {item.name}",
            }
        )
    return result


def build_meta_body(*, organization, body):
    allowed = {item["key"] for item in available_placeholders(organization=organization)}
    mapping = {}
    reverse = {}

    def repl(match):
        key = match.group(1)
        if key not in allowed:
            raise TemplateError(f"Unknown template placeholder: {key}.")
        if key not in reverse:
            number = str(len(reverse) + 1)
            reverse[key] = number
            mapping[number] = key
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

    if counts["visit_website"] > 2:
        raise TemplateError("A standard template can contain at most two Visit Website buttons.")
    if counts["call_phone"] > 1:
        raise TemplateError("A standard template can contain only one Call Phone button.")
    if counts["copy_offer"] > 1:
        raise TemplateError("A standard template can contain only one Copy Code button.")
    if counts["request_contact_info"]:
        raise TemplateError("Request Contact Info is a chat action, not a Meta message-template button.")
    return counts


def _clean_carousel_config(config):
    if not isinstance(config, dict):
        raise TemplateError("Carousel configuration must be an object.")
    raw_types = config.get("button_types") or []
    if not isinstance(raw_types, list):
        raise TemplateError("Carousel button types are invalid.")
    button_types = [str(value or "").strip() for value in raw_types[:2]]
    try:
        button_count = int(config.get("button_count") or len(button_types) or 1)
    except (TypeError, ValueError) as exc:
        raise TemplateError("Carousel button count is invalid.") from exc

    raw_cards = config.get("cards") or []
    if not isinstance(raw_cards, list):
        raise TemplateError("Carousel cards must be a list.")
    cards = []
    for index, raw in enumerate(raw_cards[:10]):
        if not isinstance(raw, dict):
            raise TemplateError("Each carousel card must be an object.")
        uid = str(raw.get("uid") or f"card_{index + 1}").strip()
        if not CARD_UID_RE.fullmatch(uid):
            uid = f"card_{index + 1}"
        buttons = raw.get("buttons") or []
        if not isinstance(buttons, list):
            buttons = []
        clean_buttons = []
        for button in buttons[:2]:
            if not isinstance(button, dict):
                button = {}
            clean_buttons.append(
                {
                    "type": str(button.get("type") or "").strip(),
                    "text": str(button.get("text") or "").strip()[:25],
                    "url": str(button.get("url") or "").strip(),
                    "phone_number": str(button.get("phone_number") or "").strip(),
                }
            )
        cards.append(
            {
                "uid": uid,
                "body": str(raw.get("body") or "")[:160],
                "media_type": str(raw.get("media_type") or "").strip().lower(),
                "header_handle": str(raw.get("header_handle") or ""),
                "media_name": str(raw.get("media_name") or "")[:255],
                "mime_type": str(raw.get("mime_type") or "")[:128],
                "file_size": raw.get("file_size") if isinstance(raw.get("file_size"), int) else None,
                "buttons": clean_buttons,
            }
        )
    return {
        "button_count": button_count,
        "button_types": button_types,
        "cards": cards,
    }


def _merge_carousel_handles(new_config, old_config):
    old_cards = {
        card.get("uid"): card
        for card in (old_config or {}).get("cards", [])
        if isinstance(card, dict) and card.get("uid")
    }
    for card in new_config.get("cards", []):
        old = old_cards.get(card["uid"]) or {}
        if old.get("media_type") != card.get("media_type"):
            continue
        card["header_handle"] = old.get("header_handle", "")
        card["media_name"] = old.get("media_name", "")
        card["mime_type"] = old.get("mime_type", "")
        card["file_size"] = old.get("file_size")
    return new_config


def _validate_carousel_config(*, template, config, for_submit=False, require_handles=False):
    config = _clean_carousel_config(config)
    cards = config["cards"]
    button_count = config["button_count"]
    button_types = config["button_types"]

    if button_count not in {1, 2}:
        raise TemplateError("Carousel templates support one or two buttons per card.")
    if len(cards) > 10:
        raise TemplateError("Carousel templates support a maximum of 10 cards.")
    for card in cards:
        if len(card["body"]) > 160:
            raise TemplateError("Carousel card body text cannot exceed 160 characters.")

    if not for_submit:
        return config

    if template.category != WhatsAppTemplate.Category.MARKETING:
        raise TemplateError("Carousel templates must use the Marketing category.")
    if len(cards) < 2 or len(cards) > 10:
        raise TemplateError("Carousel templates require between 2 and 10 cards.")
    if len(button_types) != button_count or any(kind not in CAROUSEL_BUTTON_TYPES for kind in button_types):
        raise TemplateError("Select a valid type for every carousel button.")

    media_types = {card["media_type"] for card in cards}
    if len(media_types) != 1 or next(iter(media_types), "") not in CAROUSEL_MEDIA_TYPES:
        raise TemplateError("Every carousel card must use the same media format: all Image or all Video.")

    bodies_present = [bool(card["body"].strip()) for card in cards]
    if any(bodies_present) and not all(bodies_present):
        raise TemplateError("If one carousel card has body text, body text must be added to every card.")

    for card_number, card in enumerate(cards, start=1):
        if "{{" in card["body"] or "}}" in card["body"]:
            raise TemplateError("CRM placeholders are supported in the main message; carousel card body text must be static.")
        if len(card["buttons"]) != button_count:
            raise TemplateError(f"Card {card_number} must configure {button_count} button(s).")
        for button_index, expected_type in enumerate(button_types):
            button = card["buttons"][button_index]
            if button.get("type") != expected_type:
                raise TemplateError("Every carousel card must use the same button types in the same order.")
            if not button.get("text"):
                raise TemplateError(f"Card {card_number} button {button_index + 1} needs button text.")
            if expected_type == "visit_website":
                parsed = urlparse(button.get("url") or "")
                if parsed.scheme not in {"http", "https"} or not parsed.netloc:
                    raise TemplateError(f"Card {card_number} has an invalid website URL.")
            elif expected_type == "call_phone" and not PHONE_RE.fullmatch(button.get("phone_number") or ""):
                raise TemplateError(f"Card {card_number} has an invalid international phone number.")
        if require_handles and not card.get("header_handle"):
            raise TemplateError(f"Card {card_number} needs an uploaded media sample before submission.")
    return config


def validate_template(*, template, for_submit=False, carousel_config=None):
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

    if template.template_format == WhatsAppTemplate.Format.CAROUSEL:
        _validate_carousel_config(
            template=template,
            config=carousel_config or {},
            for_submit=for_submit,
        )
    else:
        _validate_button_set(template.buttons)

    if for_submit:
        account = template.account
        if account.status != WhatsAppAccount.Status.CONNECTED or not account.is_active:
            raise TemplateError("Selected WhatsApp business is not connected.")
        if not account.waba_id or not account.access_token:
            raise TemplateError("Selected business is missing WABA credentials.")
    try:
        template.full_clean(exclude=["status"])
    except ValidationError as exc:
        raise TemplateError(str(exc)) from exc


def _button(item):
    kind = item.get("type")
    text = (item.get("text") or "").strip()
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
    order = ("visit_website", "call_phone", "copy_offer", "text_back")
    return [item for kind in order for item in buttons if item.get("type") == kind]


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
    """Upload a template media sample to Meta's resumable upload endpoint."""
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
            headers={**headers, "Content-Type": mime, "file_offset": "0"},
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


def _carousel_button_payload(button):
    kind = button["type"]
    if kind == "text_back":
        return {"type": "QUICK_REPLY", "text": button["text"][:25]}
    if kind == "visit_website":
        return {"type": "URL", "text": button["text"][:25], "url": button["url"]}
    if kind == "call_phone":
        return {
            "type": "PHONE_NUMBER",
            "text": button["text"][:25],
            "phone_number": button["phone_number"],
        }
    raise TemplateError("Unsupported carousel button type.")


def _upload_carousel_samples(*, template, config, carousel_files):
    files = carousel_files or {}
    st = state_for(template)
    for card in config["cards"]:
        field_name = f"carousel_media_{card['uid']}"
        uploaded_file = files.get(field_name)
        if uploaded_file is not None:
            handle, mime, size = _upload_header_sample(
                account=template.account,
                attachment_type=card["media_type"],
                uploaded_file=uploaded_file,
            )
            card["header_handle"] = handle
            card["media_name"] = getattr(uploaded_file, "name", "")[:255]
            card["mime_type"] = mime
            card["file_size"] = size
            st.carousel_config = config
            st.save(update_fields=["carousel_config", "updated_at"])
    return _validate_carousel_config(
        template=template,
        config=config,
        for_submit=True,
        require_handles=True,
    )


def _build_carousel_components(*, template, config):
    config = _validate_carousel_config(
        template=template,
        config=config,
        for_submit=True,
        require_handles=True,
    )
    body, mapping = build_meta_body(organization=template.organization, body=template.body)
    cards_payload = []
    for card in config["cards"]:
        card_components = [
            {
                "type": "HEADER",
                "format": card["media_type"].upper(),
                "example": {"header_handle": [card["header_handle"]]},
            }
        ]
        if card["body"].strip():
            card_components.append({"type": "BODY", "text": card["body"].strip()})
        card_components.append(
            {
                "type": "BUTTONS",
                "buttons": [_carousel_button_payload(item) for item in card["buttons"]],
            }
        )
        cards_payload.append({"components": card_components})
    components = [
        {"type": "BODY", "text": body},
        {"type": "CAROUSEL", "cards": cards_payload},
    ]
    return components, mapping, config


def build_meta_payload(*, template, header_handle="", carousel_config=None):
    st = state_for(template)
    if template.template_format == WhatsAppTemplate.Format.CAROUSEL:
        components, mapping, clean_config = _build_carousel_components(
            template=template,
            config=carousel_config or st.carousel_config,
        )
        st.carousel_config = clean_config
    else:
        body, mapping = build_meta_body(organization=template.organization, body=template.body)
        components = []
        if template.attachment_type != WhatsAppTemplate.AttachmentType.NONE:
            if not header_handle:
                raise TemplateError("A Meta header sample handle is required for media templates.")
            components.append(
                {
                    "type": "HEADER",
                    "format": template.attachment_type.upper(),
                    "example": {"header_handle": [header_handle]},
                }
            )
        components.append({"type": "BODY", "text": body})
        if template.footer:
            components.append({"type": "FOOTER", "text": template.footer})
        if template.buttons:
            _validate_button_set(template.buttons)
            components.append(
                {
                    "type": "BUTTONS",
                    "buttons": [_button(item) for item in _ordered_buttons(template.buttons)],
                }
            )
    st.placeholder_mapping = mapping
    st.components = components
    st.save(update_fields=["placeholder_mapping", "components", "carousel_config", "updated_at"])
    return {
        "name": template.name,
        "language": st.language or "en_US",
        "category": template.category.upper(),
        "components": components,
    }


def create_template(
    *,
    organization,
    account,
    created_by,
    name,
    body,
    category=WhatsAppTemplate.Category.MARKETING,
    template_format=WhatsAppTemplate.Format.STANDARD,
    footer="",
    attachment_type=WhatsAppTemplate.AttachmentType.NONE,
    buttons=None,
    language="en_US",
    carousel_config=None,
):
    is_carousel = template_format == WhatsAppTemplate.Format.CAROUSEL
    if is_carousel:
        category = WhatsAppTemplate.Category.MARKETING
        footer = ""
        attachment_type = WhatsAppTemplate.AttachmentType.NONE
        buttons = []
    clean_carousel = _clean_carousel_config(carousel_config or {}) if is_carousel else {}
    template = WhatsAppTemplate(
        organization=organization,
        account=account,
        created_by=created_by,
        name=name,
        body=body,
        category=category,
        template_format=template_format,
        footer=footer,
        attachment_type=attachment_type,
        buttons=buttons or [],
        status=WhatsAppTemplate.Status.DRAFT,
    )
    validate_template(template=template, carousel_config=clean_carousel)
    _, mapping = build_meta_body(organization=organization, body=body)
    with transaction.atomic():
        template.save()
        WhatsAppTemplateMetadata.objects.create(
            template=template,
            language=language or "en_US",
            placeholder_mapping=mapping,
            carousel_config=clean_carousel,
        )
    return template


def update_draft(
    *,
    template,
    account,
    name,
    body,
    category,
    template_format,
    footer="",
    attachment_type="none",
    buttons=None,
    language="en_US",
    carousel_config=None,
):
    if template.status != WhatsAppTemplate.Status.DRAFT or template.meta_template_id:
        raise TemplateError("Submitted templates cannot be edited in place. Copy to a draft instead.")
    st = state_for(template)
    old_attachment_type = template.attachment_type
    is_carousel = template_format == WhatsAppTemplate.Format.CAROUSEL
    clean_carousel = _clean_carousel_config(carousel_config or {}) if is_carousel else {}
    if is_carousel:
        clean_carousel = _merge_carousel_handles(clean_carousel, st.carousel_config)
        category = WhatsAppTemplate.Category.MARKETING
        footer = ""
        attachment_type = WhatsAppTemplate.AttachmentType.NONE
        buttons = []

    template.account = account
    template.name = name
    template.body = body
    template.category = category
    template.template_format = template_format
    template.footer = footer
    template.attachment_type = attachment_type
    template.buttons = buttons or []
    validate_template(template=template, carousel_config=clean_carousel)
    _, mapping = build_meta_body(organization=template.organization, body=body)
    with transaction.atomic():
        template.save()
        st.language = language or "en_US"
        st.placeholder_mapping = mapping
        st.local_status = WhatsAppTemplateMetadata.LocalStatus.DRAFT
        st.carousel_config = clean_carousel
        if is_carousel or old_attachment_type != attachment_type or attachment_type == WhatsAppTemplate.AttachmentType.NONE:
            st.header_sample_handle = ""
            st.header_file_name = ""
            st.header_mime_type = ""
            st.header_file_size = None
        st.save()
    return template


def submit_template(*, template, attachment_file=None, carousel_files=None):
    if template.status != WhatsAppTemplate.Status.DRAFT:
        raise TemplateError(f"Template is already {template.get_status_display()}.")
    st = state_for(template)
    validate_template(
        template=template,
        for_submit=True,
        carousel_config=st.carousel_config,
    )

    header_handle = ""
    carousel_config = None
    if template.template_format == WhatsAppTemplate.Format.CAROUSEL:
        carousel_config = _upload_carousel_samples(
            template=template,
            config=_clean_carousel_config(st.carousel_config),
            carousel_files=carousel_files,
        )
    elif template.attachment_type != WhatsAppTemplate.AttachmentType.NONE:
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
            st.save(
                update_fields=[
                    "header_sample_handle",
                    "header_file_name",
                    "header_mime_type",
                    "header_file_size",
                    "updated_at",
                ]
            )
        header_handle = st.header_sample_handle
        if not header_handle:
            rule = MEDIA_RULES[template.attachment_type]
            raise TemplateError(f"Choose a {rule['label'].lower()} sample file before submitting this template.")

    payload = build_meta_payload(
        template=template,
        header_handle=header_handle,
        carousel_config=carousel_config,
    )
    st.local_status = WhatsAppTemplateMetadata.LocalStatus.SUBMITTING
    st.save(update_fields=["local_status", "updated_at"])
    try:
        client = WhatsAppClient(template.account.phone_number_id, template.account.access_token)
        response = client._post(f"{template.account.waba_id}/message_templates", payload)
    except WhatsAppAPIError as exc:
        err = _error(exc)
        st.local_status = WhatsAppTemplateMetadata.LocalStatus.SYNC_ERROR
        st.meta_error_code = err["code"]
        st.meta_error_subcode = err["subcode"]
        st.meta_error_type = err["type"]
        st.meta_error_message = err["message"]
        st.meta_response = err["payload"]
        st.save()
        template.rejection_reason = err["message"]
        template.save(update_fields=["rejection_reason", "updated_at"])
        _audit(template, WhatsAppTemplateOperation.Operation.SUBMIT, False, error=err)
        raise TemplateError(
            err["message"],
            status_code=err["http_status"],
            meta_error_code=err["code"],
        ) from exc

    template.meta_template_id = str(response.get("id") or "")
    template.status = normalize_meta_status(response.get("status") or "PENDING")
    template.rejection_reason = ""
    template.save(update_fields=["meta_template_id", "status", "rejection_reason", "updated_at"])
    st.local_status = WhatsAppTemplateMetadata.LocalStatus.SUBMITTED
    st.submitted_at = timezone.now()
    st.meta_response = response
    st.meta_error_code = ""
    st.meta_error_subcode = ""
    st.meta_error_type = ""
    st.meta_error_message = ""
    st.save()
    _audit(template, WhatsAppTemplateOperation.Operation.SUBMIT, True, payload=response)
    return template


def normalize_meta_status(value):
    value = (value or "").strip().lower().replace(" ", "_")
    if value == "disabled":
        return WhatsAppTemplate.Status.PAUSED
    allowed = {item for item, _ in WhatsAppTemplate.Status.choices}
    return value if value in allowed else WhatsAppTemplate.Status.PENDING


def _remote_templates(account):
    if not account.waba_id or not account.access_token:
        raise TemplateError("Selected business is missing WABA credentials.")
    url = f"{meta.GRAPH_API_BASE}/{account.waba_id}/message_templates"
    params = {
        "fields": "id,name,status,category,language,components,rejected_reason",
        "limit": 100,
    }
    headers = {"Authorization": f"Bearer {account.access_token}"}
    rows = []
    while url:
        try:
            response = meta.requests.get(
                url,
                headers=headers,
                params=params,
                timeout=meta.REQUEST_TIMEOUT_SECONDS,
            )
        except meta.requests.RequestException as exc:
            raise WhatsAppAPIError(f"Network error listing templates: {exc}") from exc
        if not response.ok:
            raise WhatsAppAPIError("WhatsApp template listing failed.", response.status_code, response.text)
        try:
            data = response.json()
        except ValueError as exc:
            raise WhatsAppAPIError(
                "WhatsApp template listing returned invalid JSON.",
                response.status_code,
                response.text,
            ) from exc
        rows.extend(data.get("data") or [])
        url = (data.get("paging") or {}).get("next")
        params = None
    return rows


def _component_text(components, kind):
    for item in components or []:
        if str(item.get("type", "")).upper() == kind:
            return item.get("text") or ""
    return ""


def _internal_button_from_meta(item):
    kind = str(item.get("type") or "").upper()
    if kind == "QUICK_REPLY":
        return {"type": "text_back", "text": item.get("text") or "", "url": "", "phone_number": ""}
    if kind == "URL":
        return {"type": "visit_website", "text": item.get("text") or "", "url": item.get("url") or "", "phone_number": ""}
    if kind == "PHONE_NUMBER":
        return {"type": "call_phone", "text": item.get("text") or "", "url": "", "phone_number": item.get("phone_number") or ""}
    return None


def _carousel_from_components(components):
    carousel = next(
        (item for item in components or [] if str(item.get("type") or "").upper() == "CAROUSEL"),
        None,
    )
    if not carousel:
        return {}
    cards = []
    shared_types = []
    for index, remote_card in enumerate(carousel.get("cards") or []):
        card_components = remote_card.get("components") or []
        header = next(
            (item for item in card_components if str(item.get("type") or "").upper() == "HEADER"),
            {},
        )
        body = _component_text(card_components, "BODY")
        buttons_component = next(
            (item for item in card_components if str(item.get("type") or "").upper() == "BUTTONS"),
            {},
        )
        buttons = []
        for button in buttons_component.get("buttons") or []:
            parsed = _internal_button_from_meta(button)
            if parsed:
                buttons.append(parsed)
        if not shared_types and buttons:
            shared_types = [item["type"] for item in buttons]
        cards.append(
            {
                "uid": f"remote_{index + 1}",
                "body": body[:160],
                "media_type": str(header.get("format") or "").lower(),
                "header_handle": "",
                "media_name": "",
                "mime_type": "",
                "file_size": None,
                "buttons": buttons,
            }
        )
    return {
        "button_count": len(shared_types) or 1,
        "button_types": shared_types,
        "cards": cards,
    }


def sync_templates(*, organization, account):
    if account.organization_id != organization.id:
        raise TemplateError("Selected business does not belong to this organization.")
    try:
        remote = _remote_templates(account)
    except WhatsAppAPIError as exc:
        err = _error(exc)
        raise TemplateError(
            err["message"],
            status_code=err["http_status"],
            meta_error_code=err["code"],
        ) from exc

    created = 0
    updated = 0
    unchanged = 0
    seen = set()
    now = timezone.now()
    with transaction.atomic():
        for item in remote:
            meta_id = str(item.get("id") or "")
            name = (item.get("name") or "").strip()
            if not name:
                continue
            if meta_id:
                seen.add(meta_id)
            template = (
                WhatsAppTemplate.objects.filter(
                    organization=organization,
                    account=account,
                    meta_template_id=meta_id,
                ).first()
                if meta_id
                else None
            )
            template = template or WhatsAppTemplate.objects.filter(
                organization=organization,
                account=account,
                name=name,
            ).first()
            components = item.get("components") or []
            carousel_config = _carousel_from_components(components)
            template_format = (
                WhatsAppTemplate.Format.CAROUSEL
                if carousel_config
                else WhatsAppTemplate.Format.STANDARD
            )
            category = str(item.get("category") or "MARKETING").lower()
            if carousel_config:
                category = WhatsAppTemplate.Category.MARKETING

            if template is None:
                template = WhatsAppTemplate.objects.create(
                    organization=organization,
                    account=account,
                    name=name,
                    category=category,
                    template_format=template_format,
                    status=normalize_meta_status(item.get("status")),
                    body=_component_text(components, "BODY"),
                    footer="" if carousel_config else _component_text(components, "FOOTER")[:60],
                    attachment_type=WhatsAppTemplate.AttachmentType.NONE,
                    meta_template_id=meta_id,
                    rejection_reason=item.get("rejected_reason") or "",
                )
                WhatsAppTemplateMetadata.objects.create(
                    template=template,
                    local_status=WhatsAppTemplateMetadata.LocalStatus.SYNCED,
                    language=item.get("language") or "en_US",
                    components=components,
                    carousel_config=carousel_config,
                    meta_response=item,
                    last_synced_at=now,
                )
                _audit(template, WhatsAppTemplateOperation.Operation.SYNC, True, payload=item)
                created += 1
            else:
                before = (
                    template.status,
                    template.category,
                    template.rejection_reason,
                    template.meta_template_id,
                    template.template_format,
                )
                template.meta_template_id = meta_id or template.meta_template_id
                template.status = normalize_meta_status(item.get("status"))
                template.category = category
                template.template_format = template_format
                template.rejection_reason = item.get("rejected_reason") or ""
                template.save(
                    update_fields=[
                        "meta_template_id",
                        "status",
                        "category",
                        "template_format",
                        "rejection_reason",
                        "updated_at",
                    ]
                )
                st = state_for(template)
                st.local_status = WhatsAppTemplateMetadata.LocalStatus.SYNCED
                st.language = item.get("language") or st.language
                st.components = components
                st.carousel_config = carousel_config
                st.meta_response = item
                st.last_synced_at = now
                if template.status == WhatsAppTemplate.Status.APPROVED and not st.approved_at:
                    st.approved_at = now
                if template.status == WhatsAppTemplate.Status.REJECTED and not st.rejected_at:
                    st.rejected_at = now
                st.save()
                after = (
                    template.status,
                    template.category,
                    template.rejection_reason,
                    template.meta_template_id,
                    template.template_format,
                )
                changed = before != after
                updated += int(changed)
                unchanged += int(not changed)
                _audit(template, WhatsAppTemplateOperation.Operation.SYNC, True, payload=item)

        for template in (
            WhatsAppTemplate.objects.filter(organization=organization, account=account)
            .exclude(meta_template_id="")
            .exclude(status=WhatsAppTemplate.Status.DRAFT)
        ):
            if template.meta_template_id not in seen:
                st = state_for(template)
                st.local_status = WhatsAppTemplateMetadata.LocalStatus.REMOTE_DELETED
                st.last_synced_at = now
                st.save(update_fields=["local_status", "last_synced_at", "updated_at"])
    return {
        "created": created,
        "updated": updated,
        "unchanged": unchanged,
        "failed": 0,
        "total": len(remote),
    }


def _carousel_copy_config(config):
    copied = copy.deepcopy(config or {})
    for card in copied.get("cards", []):
        if isinstance(card, dict):
            card["header_handle"] = ""
            card["media_name"] = ""
            card["mime_type"] = ""
            card["file_size"] = None
    return copied


def copy_template(*, template, created_by):
    base = f"{template.name}_copy"
    name = base
    number = 2
    while WhatsAppTemplate.objects.filter(account=template.account, name=name).exists():
        name = f"{base}_{number}"
        number += 1
    source = state_for(template)
    with transaction.atomic():
        copied = WhatsAppTemplate.objects.create(
            organization=template.organization,
            account=template.account,
            name=name,
            category=template.category,
            template_format=template.template_format,
            status=WhatsAppTemplate.Status.DRAFT,
            body=template.body,
            footer=template.footer,
            attachment_type=template.attachment_type,
            buttons=template.buttons,
            created_by=created_by,
        )
        WhatsAppTemplateMetadata.objects.create(
            template=copied,
            language=source.language,
            placeholder_mapping=source.placeholder_mapping,
            carousel_config=_carousel_copy_config(source.carousel_config),
        )
        _audit(
            copied,
            WhatsAppTemplateOperation.Operation.COPY,
            True,
            payload={"copied_from": str(template.id)},
        )
    return copied


def delete_template(*, template):
    if not template.meta_template_id:
        _audit(template, WhatsAppTemplateOperation.Operation.DELETE, True, payload={"local_draft": True})
        template.delete()
        return
    account = template.account
    url = f"{meta.GRAPH_API_BASE}/{account.waba_id}/message_templates"
    try:
        response = meta.requests.delete(
            url,
            headers={"Authorization": f"Bearer {account.access_token}"},
            params={"name": template.name, "hsm_id": template.meta_template_id},
            timeout=meta.REQUEST_TIMEOUT_SECONDS,
        )
    except meta.requests.RequestException as exc:
        raise TemplateError(f"Network error deleting WhatsApp template: {exc}") from exc
    if not response.ok and response.status_code != 404:
        err = _error(WhatsAppAPIError("WhatsApp template deletion failed.", response.status_code, response.text))
        _audit(template, WhatsAppTemplateOperation.Operation.DELETE, False, error=err)
        raise TemplateError(
            err["message"],
            status_code=err["http_status"],
            meta_error_code=err["code"],
        )
    try:
        payload = response.json()
    except ValueError:
        payload = {"deleted": True}
    _audit(template, WhatsAppTemplateOperation.Operation.DELETE, True, payload=payload)
    template.status = WhatsAppTemplate.Status.PENDING_DELETION
    template.save(update_fields=["status", "updated_at"])
    st = state_for(template)
    st.local_status = WhatsAppTemplateMetadata.LocalStatus.DELETED
    st.deleted_at = timezone.now()
    st.meta_response = payload
    st.save()


def render_template_body(*, template, lead, user=None):
    values = {
        "lead_name": lead.name,
        "lead_first_name": (lead.name or "").split(" ")[0],
        "phone": lead.phone,
        "email": lead.email or "",
        "lead_source": getattr(lead, "lead_source", "") or "",
        "org_name": lead.organization.name,
        "user_name": getattr(user, "name", "") or getattr(user, "email", "") or "",
        "pipeline_name": lead.pipeline.name if lead.pipeline_id else "",
        "stage_name": lead.stage.name if lead.stage_id else "",
    }
    values.update(getattr(lead, "attributes", None) or {})
    return VAR_RE.sub(lambda match: str(values.get(match.group(1), match.group(0))), template.body)


# Backward-compatible name used by older views/templates.
AVAILABLE_VARIABLES = []
