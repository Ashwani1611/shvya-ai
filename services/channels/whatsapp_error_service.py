"""Normalize Meta WhatsApp delivery failures for storage and chat display."""

import json
import re


ERROR_CATALOG = {
    "131049": {
        "title": "Marketing Message Limited",
        "why": "Meta limited the marketing message under Healthy Ecosystem protections.",
        "resolve": "Retry later and improve message quality and recipient engagement.",
    },
    "131026": {
        "title": "Message Undeliverable",
        "why": "The message could not be delivered to the recipient.",
        "resolve": "Verify the number is active, correctly formatted, and registered on WhatsApp.",
    },
    "131047": {
        "title": "Customer Service Window Expired",
        "why": "The 24-hour customer-service window has expired.",
        "resolve": "Send an approved WhatsApp template instead of a free-form message.",
    },
    "132000": {
        "title": "Template Parameter Mismatch",
        "why": "Template variables do not match the approved template definition.",
        "resolve": "Check the variable count, order, types, and values, then retry.",
    },
    "132001": {
        "title": "Template Unavailable",
        "why": "The requested template is unavailable or could not be found.",
        "resolve": "Check the template name, language, WhatsApp account, and approval status.",
    },
    "132007": {
        "title": "Template Disabled or Deleted",
        "why": "The selected template has been disabled or deleted on Meta.",
        "resolve": "Select another approved template or create and approve a replacement.",
    },
    "132015": {
        "title": "Template Paused",
        "why": "Meta paused the template because of low quality or negative engagement.",
        "resolve": "Improve the template content and recipient engagement before using it again.",
    },
    "132016": {
        "title": "Template Disabled",
        "why": "Meta disabled the template because of poor quality.",
        "resolve": "Create and submit a higher-quality replacement template for approval.",
    },
    "131031": {
        "title": "WhatsApp Account Restricted",
        "why": "The WhatsApp Business Account is restricted from sending this message.",
        "resolve": "Check Meta Business Manager restrictions and resolve any policy issues.",
    },
    "131005": {
        "title": "Permission Denied",
        "why": "Meta denied API access or a required permission is missing.",
        "resolve": "Verify the access token, app permissions, and WhatsApp account access.",
    },
    "131008": {
        "title": "Missing Required Parameter",
        "why": "A required parameter is missing from the WhatsApp API request.",
        "resolve": "Add the missing required parameter and retry the message.",
    },
    "131009": {
        "title": "Invalid Parameter",
        "why": "A WhatsApp API parameter contains an invalid value or format.",
        "resolve": "Correct the invalid parameter value or format and retry.",
    },
    "130429": {
        "title": "Messaging Rate Limit Reached",
        "why": "The WhatsApp messaging rate limit was reached.",
        "resolve": "Reduce sending frequency and retry after the rate limit recovers.",
    },
    "131042": {
        "title": "Billing or Payment Issue",
        "why": "The WhatsApp Business Account has a billing or payment problem.",
        "resolve": "Check the WABA billing and payment status in Meta Business Manager.",
    },
    "131030": {
        "title": "Recipient Not Allowed",
        "why": "The recipient is not allowed for this WhatsApp sender or test configuration.",
        "resolve": "Add the recipient to the allowed/test recipient list or use an eligible production recipient.",
    },
}

_CODE_RE = re.compile(r"(?<!\d)(1\d{5})(?!\d)")


def _json_object(value):
    if isinstance(value, dict):
        return value
    if not isinstance(value, str) or not value.strip():
        return {}
    try:
        parsed = json.loads(value)
    except (TypeError, ValueError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _first_error(payload):
    """Return one Meta error object from API or status-webhook shapes."""
    if not isinstance(payload, dict):
        return {}

    direct = payload.get("meta_error")
    if isinstance(direct, dict):
        return direct

    error = payload.get("error")
    if isinstance(error, dict):
        return error

    errors = payload.get("errors")
    if isinstance(errors, list):
        for item in errors:
            if isinstance(item, dict):
                return item

    stored_response = payload.get("meta_error_response")
    if isinstance(stored_response, dict):
        return _first_error(stored_response)

    return {}


def extract_meta_error(*, raw_payload=None, error_text=""):
    """Extract code/title/message/details without assuming one Meta payload shape."""
    payload = raw_payload if isinstance(raw_payload, dict) else {}
    error = _first_error(payload)

    if not error and error_text:
        parsed = _json_object(error_text)
        error = _first_error(parsed)

    error_data = error.get("error_data") if isinstance(error, dict) else {}
    if not isinstance(error_data, dict):
        error_data = {}

    code = str(error.get("code") or "").strip() if error else ""
    title = str(error.get("title") or error.get("type") or "").strip() if error else ""
    message = str(error.get("message") or "").strip() if error else ""
    details = str(error_data.get("details") or error.get("details") or "").strip() if error else ""

    if not code:
        search_text = " ".join(
            value for value in (error_text, message, details, title) if value
        )
        match = _CODE_RE.search(search_text)
        if match:
            code = match.group(1)

    return {
        "code": code,
        "meta_title": title,
        "message": message,
        "details": details,
    }


def describe_whatsapp_failure(*, raw_payload=None, error_text=""):
    """Return the concise Code / Why / Resolve structure used by the chat UI."""
    extracted = extract_meta_error(raw_payload=raw_payload, error_text=error_text)
    code = extracted["code"]
    known = ERROR_CATALOG.get(code, {})
    meta_message = extracted["details"] or extracted["message"] or error_text or ""

    if known:
        title = known["title"]
        why = known["why"]
        resolve = known["resolve"]
    else:
        title = extracted["meta_title"] or "WhatsApp Delivery Failed"
        why = meta_message or "Meta reported that WhatsApp could not deliver this message."
        resolve = (
            "Review the Meta error details, verify the recipient, template, account, and request data, then retry."
        )

    return {
        "code": code,
        "title": title,
        "why": why,
        "resolve": resolve,
        "meta_message": meta_message,
    }


def message_failure_details(message):
    return describe_whatsapp_failure(
        raw_payload=getattr(message, "raw_payload", None),
        error_text=getattr(message, "error", "") or "",
    )


def failure_summary(details):
    code = details.get("code") or ""
    title = details.get("title") or "WhatsApp Delivery Failed"
    return f"{code} — {title}" if code else title


def merge_api_error_payload(existing_payload, response_body):
    """Persist Meta's immediate API error body without discarding SHVYA metadata."""
    payload = dict(existing_payload) if isinstance(existing_payload, dict) else {}
    response = _json_object(response_body)
    if response:
        payload["meta_error_response"] = response
    elif response_body:
        payload["meta_error_response"] = {"message": str(response_body)[:4000]}
    return payload
