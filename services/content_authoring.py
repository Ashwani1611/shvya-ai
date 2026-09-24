"""Canonical customer-facing content authoring and personalization rules."""

from __future__ import annotations

import html
import re

from django.utils.html import strip_tags

from apps.crm.models.attribute import AttributeDefinition


DOUBLE_PLACEHOLDER_RE = re.compile(r"\{\{\s*([^{}]+?)\s*\}\}")
SINGLE_PLACEHOLDER_RE = re.compile(r"(?<!\{)\{\s*([A-Za-z0-9_]+)\s*\}(?!\})")
PLACEHOLDER_KEY_RE = re.compile(r"^[A-Za-z0-9_]+$")
MARKDOWN_LINK_RE = re.compile(r"\[([^\]\n]+)\]\(([^)\n]+)\)")
MARKDOWN_IMAGE_RE = re.compile(r"!\[([^\]\n]*)\]\(([^)\n]+)\)")
HEADING_RE = re.compile(r"^\s{0,3}#{1,6}\s+", re.MULTILINE)
BLOCKQUOTE_RE = re.compile(r"^\s*>\s?", re.MULTILINE)
BULLET_RE = re.compile(r"^\s*[-*+]\s+", re.MULTILINE)
HORIZONTAL_RULE_RE = re.compile(r"^\s*(?:-{3,}|\*{3,}|_{3,})\s*$", re.MULTILINE)
FENCE_RE = re.compile(r"^\s*\x60\x60\x60[^\n]*$", re.MULTILINE)
HTML_BREAK_RE = re.compile(
    r"<\s*(?:br\s*/?|/p|/div|/li|/h[1-6])\s*>",
    re.IGNORECASE,
)


BUILTIN_PLACEHOLDERS = (
    ("lead_name", "Lead name", "lead", "name", "John Smith"),
    ("lead_first_name", "Lead first name", "lead", "name", "John"),
    ("phone", "Phone", "lead", "phone", "+919876543210"),
    ("email", "Email", "lead", "email", "lead@example.com"),
    ("lead_source", "Lead source", "lead", "lead_source", "Website"),
    ("pipeline_name", "Pipeline", "lead", "pipeline", "Sales"),
    ("stage_name", "Stage", "lead", "stage", "Qualified"),
    ("org_name", "Organization name", "organization", "name", "Organization"),
    ("user_name", "User name", "user", "name", "Team member"),
)


class ContentAuthoringError(ValueError):
    pass


def available_placeholders(*, organization):
    """Return the tenant-safe personalization variables supported by SHVYA."""

    result = []
    for key, label, source, field_name, example in BUILTIN_PLACEHOLDERS:
        if key == "org_name":
            example = organization.name
        result.append(
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
        )
    for item in AttributeDefinition.objects.filter(
        is_active=True,
        organization=organization,
    ):
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


def supported_placeholder_keys(*, organization):
    return {
        item["key"]
        for item in available_placeholders(organization=organization)
    }


def _canonicalize_placeholders(text, *, organization, field, allow_placeholders):
    allowed = (
        supported_placeholder_keys(organization=organization)
        if allow_placeholders
        else set()
    )

    def double_repl(match):
        raw_key = match.group(1).strip()
        if not PLACEHOLDER_KEY_RE.fullmatch(raw_key):
            raise ContentAuthoringError(
                f"{field} contains an invalid placeholder: {raw_key}."
            )
        if not allow_placeholders:
            raise ContentAuthoringError(
                f"{field} does not support placeholders."
            )
        if raw_key not in allowed:
            raise ContentAuthoringError(
                f"{field} contains unsupported placeholder {{{{{raw_key}}}}}."
            )
        return "{{" + raw_key + "}}"

    text = DOUBLE_PLACEHOLDER_RE.sub(double_repl, text)

    def single_repl(match):
        key = match.group(1)
        if not allow_placeholders:
            raise ContentAuthoringError(
                f"{field} does not support placeholders."
            )
        if key not in allowed:
            raise ContentAuthoringError(
                f"{field} contains unsupported placeholder {{{key}}}."
            )
        return "{{" + key + "}}"

    return SINGLE_PLACEHOLDER_RE.sub(single_repl, text)


def _plain_text_markup(text):
    """Convert common HTML/Markdown authoring into literal plain text."""

    text = HTML_BREAK_RE.sub("\n", text)
    text = html.unescape(strip_tags(text))
    text = MARKDOWN_IMAGE_RE.sub(
        lambda match: (
            f"{match.group(1).strip()} ({match.group(2).strip()})"
            if match.group(1).strip()
            else match.group(2).strip()
        ),
        text,
    )
    text = MARKDOWN_LINK_RE.sub(
        lambda match: f"{match.group(1).strip()} ({match.group(2).strip()})",
        text,
    )
    text = FENCE_RE.sub("", text)
    text = HEADING_RE.sub("", text)
    text = BLOCKQUOTE_RE.sub("", text)
    text = BULLET_RE.sub("• ", text)
    text = HORIZONTAL_RULE_RE.sub("", text)
    text = text.replace("**", "").replace("__", "").replace("~~", "")
    text = re.sub(r"(?<!\w)\*(?=\S)([^*\n]*?\S)\*(?!\w)", r"\1", text)
    text = re.sub(r"(?<!\w)_(?=\S)([^_\n]*?\S)_(?!\w)", r"\1", text)
    text = re.sub(r"(?<!\w)~(?=\S)([^~\n]*?\S)~(?!\w)", r"\1", text)
    text = text.replace(chr(96), "")
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    text = "\n".join(line.rstrip() for line in text.split("\n"))
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def normalize_plain_text(
    value,
    *,
    organization,
    field="Content",
    allow_placeholders=True,
    required=False,
    max_length=None,
):
    """Normalize customer-facing authoring and validate supported placeholders."""

    text = str(value or "")
    text = _canonicalize_placeholders(
        text,
        organization=organization,
        field=field,
        allow_placeholders=allow_placeholders,
    )

    placeholders = []

    def protect(match):
        placeholders.append(match.group(0))
        return f"\x00SHVYA_PH_{len(placeholders) - 1}\x00"

    protected = DOUBLE_PLACEHOLDER_RE.sub(protect, text)
    protected = _plain_text_markup(protected)

    for index, placeholder in enumerate(placeholders):
        protected = protected.replace(
            f"\x00SHVYA_PH_{index}\x00",
            placeholder,
        )

    text = protected.strip()
    if required and not text:
        raise ContentAuthoringError(f"{field} is required.")
    if max_length is not None and len(text) > max_length:
        raise ContentAuthoringError(
            f"{field} cannot exceed {max_length} characters."
        )
    return text


def personalization_values(*, lead, user=None):
    values = {
        "lead_name": lead.name or "",
        "lead_first_name": (lead.name or "").split(" ")[0],
        "phone": lead.phone or "",
        "email": lead.email or "",
        "lead_source": getattr(lead, "lead_source", "") or "",
        "org_name": lead.organization.name,
        "user_name": getattr(user, "name", "") or getattr(user, "email", "") or "",
        "pipeline_name": lead.pipeline.name if lead.pipeline_id else "",
        "stage_name": lead.stage.name if lead.stage_id else "",
    }
    for key, value in (getattr(lead, "attributes", None) or {}).items():
        values.setdefault(str(key), value)
    return values


def render_personalized_text(text, *, lead, user=None):
    values = personalization_values(lead=lead, user=user)

    def repl(match):
        key = match.group(1).strip()
        return str(values.get(key, match.group(0)) or "")

    rendered = DOUBLE_PLACEHOLDER_RE.sub(repl, str(text or ""))
    return SINGLE_PLACEHOLDER_RE.sub(
        lambda match: str(values.get(match.group(1), match.group(0)) or ""),
        rendered,
    )
