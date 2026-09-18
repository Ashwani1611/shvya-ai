"""Pure, fail-closed policies shared by campaign preview and delivery.

No network requests, database writes, template execution, or invented values.
"""
from __future__ import annotations

import hashlib
import ipaddress
import json
import re
from datetime import datetime, timedelta, timezone
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

VARIABLE = re.compile(r"{{\s*([A-Za-z_][A-Za-z0-9_]*|\d+)\s*}}")
TRANSIENT_CODES = {"130429", "131000", "131016", "131049", "131056"}
MAX_RETRIES = 3


class CampaignInputError(ValueError):
    """An actionable configuration error, safe to display to the operator."""


def integer(value, *, minimum, maximum, label):
    if isinstance(value, bool) or len(str(value)) > 9 or not re.fullmatch(r"\d+", str(value)):
        raise CampaignInputError(f"{label} must be a whole number.")
    result = int(value)
    if not minimum <= result <= maximum:
        raise CampaignInputError(f"{label} must be between {minimum} and {maximum}.")
    return result


def scheduled_time(value, zone, *, now):
    """Reject DST gaps/ambiguities rather than silently moving a campaign."""
    try:
        tz = ZoneInfo(str(zone))
        local = datetime.fromisoformat(str(value))
    except (ValueError, TypeError, ZoneInfoNotFoundError) as exc:
        raise CampaignInputError("Choose a valid date, time and IANA time zone.") from exc
    if local.tzinfo is not None:
        raise CampaignInputError("Enter local time without a UTC offset; choose its time zone separately.")
    aware = local.replace(tzinfo=tz, fold=0)
    if aware.astimezone(timezone.utc).astimezone(tz).replace(tzinfo=None) != local:
        raise CampaignInputError("This local time does not exist because the clocks change. Choose another time.")
    if aware.utcoffset() != local.replace(tzinfo=tz, fold=1).utcoffset():
        raise CampaignInputError("This time occurs twice when the clocks change. Choose an unambiguous time.")
    result = aware.astimezone(timezone.utc)
    if result <= now:
        raise CampaignInputError("Scheduled time must be in the future.")
    if result > now + timedelta(days=366):
        raise CampaignInputError("Schedule within the next 366 days.")
    return result


def fingerprint(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()).hexdigest()


def _tokens(text):
    tokens = list(dict.fromkeys(VARIABLE.findall(str(text or ""))))
    return sorted(tokens, key=int) if tokens and all(token.isdigit() for token in tokens) else tokens


def _groups(components, prefix=""):
    if not isinstance(components, list) or not components:
        raise CampaignInputError("Template definition is missing. Sync it from Meta first.")
    for component in components:
        if not isinstance(component, dict):
            raise CampaignInputError("Template definition is invalid. Sync it from Meta first.")
        kind = str(component.get("type", "")).lower()
        if kind == "carousel":
            for index, card in enumerate(component.get("cards") or []):
                yield from _groups(card.get("components"), f"card{index}.")
        elif kind in {"header", "body", "buttons", "footer"}:
            yield prefix, kind, component
        else:
            raise CampaignInputError(f"This template component is not supported by Bulk Campaigns: {kind or 'unknown'}.")


def template_fields(spec):
    """Describe actual Meta parameters, including headers, buttons and cards."""
    fields = []
    mapping = spec.get("placeholder_mapping") or {}
    for prefix, kind, component in _groups(spec.get("components")):
        label_prefix = f"Card {int(prefix[4:-1]) + 1} · " if prefix else ""
        if kind in {"body", "header"}:
            fmt = str(component.get("format", "TEXT")).lower()
            if kind == "header" and fmt != "text":
                if fmt not in {"image", "video", "document"}:
                    raise CampaignInputError(f"{fmt.title()} headers are not supported by Bulk Campaigns.")
                fields.append({"key": f"{prefix}header.media", "label": f"{label_prefix}{fmt.title()} header", "kind": fmt, "source": ""})
            else:
                for token in _tokens(component.get("text")):
                    source = str(mapping.get(token, token)) if kind == "body" else ""
                    fields.append({"key": f"{prefix}{kind}.{token}", "label": f"{label_prefix}{kind.title()} · {source or token}", "kind": "text", "source": source})
        if kind == "footer" and _tokens(component.get("text")):
            raise CampaignInputError("Template footers cannot contain variable parameters.")
        if kind == "buttons":
            for index, button in enumerate(component.get("buttons") or []):
                btype = str(button.get("type", "")).upper()
                if btype == "URL":
                    tokens = _tokens(button.get("url"))
                    if len(tokens) > 1:
                        raise CampaignInputError("A dynamic URL button must contain at most one parameter.")
                    for token in tokens:
                        fields.append({"key": f"{prefix}button{index}.{token}", "label": f"{label_prefix}{button.get('text') or 'URL button'} · suffix", "kind": "text", "source": ""})
                elif btype == "COPY_CODE":
                    fields.append({"key": f"{prefix}button{index}.code", "label": f"{label_prefix}Coupon code", "kind": "text", "source": ""})
                elif btype not in {"QUICK_REPLY", "PHONE_NUMBER"}:
                    raise CampaignInputError(f"{btype or 'Unknown'} buttons need a dedicated sending flow and cannot be bulk sent here.")
    if len({field["key"] for field in fields}) != len(fields):
        raise CampaignInputError("Template contains duplicate parameter definitions. Sync it again.")
    return fields


def media_parameter(value, kind):
    """The server never fetches user URLs. Meta validates the media at send time."""
    if re.fullmatch(r"id:\d{5,128}", value):
        return {"type": kind, kind: {"id": value[3:]}}
    try:
        parsed = urlsplit(value)
        hostname = (parsed.hostname or "").lower()
        valid = parsed.scheme == "https" and parsed.port in (None, 443) and not parsed.username and not parsed.password
        valid = valid and "." in hostname and not hostname.endswith((".local", ".internal", ".localhost")) and hostname != "localhost"
        try:
            valid = valid and ipaddress.ip_address(hostname).is_global
        except ValueError:
            pass
    except ValueError:
        valid = False
    if not valid:
        raise CampaignInputError("Media needs a public HTTPS URL or an account-accessible Meta media ID in the form id:123456.")
    return {"type": kind, kind: {"link": value}}


def render_message(spec, bindings, values, allowed_sources):
    """Return the exact preview body and message-time Meta components."""
    fields = template_fields(spec)
    valid_keys = {field["key"] for field in fields}
    if not isinstance(bindings, dict) or set(bindings) - valid_keys:
        raise CampaignInputError("Template parameter mapping is invalid.")
    resolved = {}
    for field in fields:
        binding = bindings.get(field["key"], {})
        if not isinstance(binding, dict):
            raise CampaignInputError("Each template parameter needs a field or a fallback value.")
        source = str(binding.get("source") or "")
        if source and source not in allowed_sources:
            raise CampaignInputError("A template parameter refers to an unavailable CRM field.")
        value = values.get(source) if source else None
        if value is None or str(value).strip() == "":
            value = binding.get("default", "")
        if isinstance(value, (dict, list)):
            raise CampaignInputError(f"{field['label']} requires a single value, not an object or list.")
        value = str(value if value is not None else "").strip()
        if not value:
            raise CampaignInputError(f"Missing value for {field['label']}. Map a populated field or enter a fallback.")
        if len(value) > 2048:
            raise CampaignInputError(f"{field['label']} exceeds 2,048 characters.")
        if field["kind"] != "text":
            media_parameter(value, field["kind"])
        resolved[field["key"]] = value

    def build(components, prefix=""):
        payload, preview = [], []
        for component in components:
            kind = str(component.get("type", "")).lower()
            if kind == "carousel":
                cards = []
                for index, card in enumerate(component.get("cards") or []):
                    parts, text = build(card["components"], f"card{index}.")
                    cards.append({"card_index": index, "components": parts})
                    preview.append(f"Card {index + 1}\n{text}")
                payload.append({"type": "carousel", "cards": cards})
            elif kind in {"header", "body", "footer"}:
                fmt = str(component.get("format", "TEXT")).lower()
                if kind == "header" and fmt != "text":
                    payload.append({"type": "header", "parameters": [media_parameter(resolved[f"{prefix}header.media"], fmt)]})
                    preview.append(f"[{fmt.title()} attachment]")
                    continue
                text = str(component.get("text") or "")
                parameters = []
                for token in _tokens(text):
                    value = resolved[f"{prefix}{kind}.{token}"]
                    parameter = {"type": "text", "text": value}
                    if not token.isdigit():
                        parameter["parameter_name"] = token
                    parameters.append(parameter)
                if parameters:
                    payload.append({"type": kind, "parameters": parameters})
                preview.append(VARIABLE.sub(lambda match: resolved[f"{prefix}{kind}.{match.group(1)}"], text))
            elif kind == "buttons":
                for index, button in enumerate(component.get("buttons") or []):
                    btype = str(button.get("type", "")).upper()
                    if btype == "URL" and _tokens(button.get("url")):
                        token = _tokens(button["url"])[0]
                        payload.append({"type": "button", "sub_type": "url", "index": str(index), "parameters": [{"type": "text", "text": resolved[f"{prefix}button{index}.{token}"]}]})
                    elif btype == "COPY_CODE":
                        payload.append({"type": "button", "sub_type": "copy_code", "index": str(index), "parameters": [{"type": "coupon_code", "coupon_code": resolved[f"{prefix}button{index}.code"]}]})
                    preview.append(f"[{button.get('text') or btype.replace('_', ' ').title()}]")
        return payload, "\n\n".join(text for text in preview if text)

    components, body = build(spec["components"])
    if not body.strip():
        raise CampaignInputError("Template has no message content.")
    return {"body": body, "components": components}


def retry_decision(*, code, http_status, uncertain, attempts, maximum_retries, failed_at, delay_hours, now):
    """One gate for automatic, individual and bulk retries; none bypass it."""
    if uncertain:
        return False, None, "Delivery is uncertain. Reconcile the original message; resending could duplicate it."
    if attempts >= 1 + maximum_retries:
        return False, None, "The campaign retry limit has been reached."
    if str(code) not in TRANSIENT_CODES and http_status != 429 and not (http_status and 500 <= http_status <= 599):
        return False, None, "This failure is not safely retryable. Resolve it and create a new campaign."
    delay = max(24 if str(code) == "131049" else 1, delay_hours)
    due = failed_at + timedelta(hours=delay)
    return True, max(now, due), "Retry will respect the configured cooldown."


def percent(value, total):
    return round(100 * value / total, 2) if total else 0.0


def csv_cell(value):
    """Spreadsheet applications must treat exported user values as text."""
    text = str(value if value is not None else "").replace("\x00", "")
    return "'" + text if text.lstrip().startswith(("=", "+", "-", "@")) or text.startswith(("\t", "\r", "\n")) else text
