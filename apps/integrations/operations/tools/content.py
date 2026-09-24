"""Customer-facing content authoring policy for SHVYA Operations MCP."""

from __future__ import annotations

from apps.integrations.operations_policy import (
    CAP_ORGANIZATION_READ,
)
from apps.integrations.operations_tools import (
    ToolExecution,
    _organization_for,
    _require_operations_capability,
)
from services.content_authoring import available_placeholders


def get_content_authoring_policy(*, identity, arguments):
    organization = _organization_for(identity)
    _require_operations_capability(
        identity=identity,
        organization=organization,
        capability=CAP_ORGANIZATION_READ,
    )
    placeholders = available_placeholders(organization=organization)
    return ToolExecution(
        data={
            "plain_text_only": True,
            "placeholder_syntax": "{{placeholder_key}}",
            "legacy_single_brace_input_normalized": True,
            "unsupported_placeholders_rejected": True,
            "applies_to": [
                "cadence",
                "touchpoint",
                "whatsapp_template",
            ],
            "placeholders": placeholders,
            "placeholder_count": len(placeholders),
            "rules": [
                "Author customer-facing content as plain text, not HTML or Markdown.",
                "Use only placeholder keys returned by this policy.",
                "Use double braces for placeholders.",
                "Cadence and Touchpoint titles/names do not support placeholders.",
                "WhatsApp template body supports placeholders; footer/button text does not.",
                "Carousel card body/button text must be static plain text.",
            ],
        },
        capability=CAP_ORGANIZATION_READ,
        target_type="organization",
        target_id=str(organization.id),
        audit_summary={
            "placeholder_count": len(placeholders),
            "plain_text_only": True,
        },
    )
