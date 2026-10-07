"""Schema contracts for channel-aware dashboard MCP operations."""

from apps.integrations.operations_policy import (
    CAP_CADENCE_CONFIG_WRITE, CAP_CHANNEL_GROUP_READ, CAP_CHANNEL_GROUP_SEND,
    CAP_MESSAGING_CONFIG_WRITE, CAP_ORGANIZATION_READ,
)


def channel_dashboard_tool_definitions(tool, write_properties):
    uid = {"type": "string", "format": "uuid"}
    limit = {"type": "integer", "minimum": 1, "maximum": 100, "default": 50}
    group = {"type": "string", "pattern": "^[0-9]+(-[0-9]+)?@g\\.us$", "maxLength": 80}
    schedule = {"type": "object", "properties": {
        "type": {"type": "string", "enum": ["immediate", "delay", "specific_time", "recurring"]},
        "delay_value": {"type": "integer", "minimum": 1},
        "delay_unit": {"type": "string", "enum": ["minutes", "hours", "days"]},
        "time": {"type": "string"}, "weekday": {"type": "integer", "minimum": 0, "maximum": 6},
        "recurring_every": {"type": "integer", "minimum": 1},
        "recurring_unit": {"type": "string", "enum": ["minutes", "hours", "days"]},
        "weekdays": {"type": "array", "items": {"type": "integer", "minimum": 0, "maximum": 6}},
    }, "additionalProperties": False}
    definitions = [
        tool("get_channel_authoring_schema", "Discover channel authoring", "List exact API, Coexistence, Hosted and Instagram sender identities, CRM placeholders and Cadence rules. With template_id returns exact delivery parameter keys and current bindings. No secrets or sends.", {"template_id": uid}),
        tool("get_channel_cadence_configuration", "Read channel Cadence", "Inspect exact sender/channel and ordered full Hosted, Instagram, API template, email and reminder step configuration. Paginate steps with next_offset; no provider calls.", {"cadence_id": uid, "offset": {"type": "integer", "minimum": 0, "maximum": 100000}, "limit": limit}, ["cadence_id"]),
        tool("upsert_channel_cadence", "Configure channel Cadence", "Create or update Cadence for one exact sender/channel. New Cadence defaults active; edits preserve activity unless explicitly changed. Does not enroll leads or send; dispatch preserves routing, opt-out, handoff and messaging windows.", write_properties({
            "cadence_id": uid, "data": {"type": "object", "additionalProperties": False, "properties": {
                "name": {"type": "string", "minLength": 1, "maxLength": 255},
                "description": {"type": "string", "maxLength": 300},
                "channel": {"type": "string", "enum": ["api", "coexistence", "hosted", "instagram"]},
                "account_id": uid, "is_active": {"type": "boolean"},
            }}}), ["data", "reason"], read_only=False),
        tool("add_channel_cadence_step", "Add channel Cadence step", "Append sender-bound approved Meta template for API/Coexistence, text for Hosted/Instagram, email or reminder. Validates CRM placeholders and schedules; no sends or enrollment.", write_properties({
            "cadence_id": uid, "data": {"type": "object", "additionalProperties": False, "properties": {
                "type": {"type": "string", "enum": ["template", "text", "email", "reminder"]},
                "title": {"type": "string", "maxLength": 255}, "body": {"type": "string", "maxLength": 10000},
                "subject": {"type": "string", "maxLength": 255}, "text": {"type": "string", "maxLength": 10000},
                "template_id": uid, "retry_count": {"type": "integer", "minimum": 0, "maximum": 5}, "schedule": schedule,
            }}}), ["cadence_id", "data", "reason"], read_only=False),
        tool("configure_whatsapp_template_delivery", "Bind template CRM variables", "Configure all delivery text parameters with tenant CRM fields or explicit fallbacks. Uses canonical delivery validation; examples are never delivery defaults. No submission or send.", write_properties({
            "template_id": uid, "bindings": {"type": "object", "additionalProperties": {"type": "object", "additionalProperties": False,
                "properties": {"source": {"type": "string"}, "default": {"type": "string", "maxLength": 2048}}}},
        }), ["template_id", "bindings", "reason"], read_only=False),
        tool("list_hosted_whatsapp_groups", "List Hosted WhatsApp groups", "Read group IDs and names through the selected tenant Hosted session. Requires channel.group.read; names are never accepted as send targets.", {"whatsapp_account_id": uid, "limit": limit}, ["whatsapp_account_id"]),
        tool("read_hosted_whatsapp_group", "Read Hosted WhatsApp group", "Read bounded text/history from the exact group in the selected tenant Hosted session. No downloads or read receipts.", {"whatsapp_account_id": uid, "group_id": group, "limit": limit}, ["whatsapp_account_id", "group_id"]),
        tool("send_hosted_whatsapp_group_message", "Send approved operations group message", "Send exactly one confirmed text message, prefixed with the selected member's name, using that member's owned Hosted sender and exact group ID. Always requires a fresh per-message dry-run approval receipt; provider request is idempotent. Never edits/deletes messages.", write_properties({
            "whatsapp_account_id": uid, "sender_member_id": uid, "group_id": group,
            "body": {"type": "string", "minLength": 1, "maxLength": 10000},
        }), ["whatsapp_account_id", "sender_member_id", "group_id", "body", "reason"], read_only=False),
    ]
    for definition in definitions:
        if definition["name"] in {"list_hosted_whatsapp_groups", "read_hosted_whatsapp_group", "send_hosted_whatsapp_group_message"}:
            definition["annotations"]["openWorldHint"] = True
    return definitions


CHANNEL_DASHBOARD_CAPABILITIES = {
    "get_channel_authoring_schema": CAP_ORGANIZATION_READ,
    "get_channel_cadence_configuration": CAP_ORGANIZATION_READ,
    "upsert_channel_cadence": CAP_CADENCE_CONFIG_WRITE,
    "add_channel_cadence_step": CAP_CADENCE_CONFIG_WRITE,
    "configure_whatsapp_template_delivery": CAP_MESSAGING_CONFIG_WRITE,
    "list_hosted_whatsapp_groups": CAP_CHANNEL_GROUP_READ,
    "read_hosted_whatsapp_group": CAP_CHANNEL_GROUP_READ,
    "send_hosted_whatsapp_group_message": CAP_CHANNEL_GROUP_SEND,
}
