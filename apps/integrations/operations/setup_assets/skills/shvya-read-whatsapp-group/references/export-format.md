# Authorized group export contract

Use a supplied, sanitized export for one organization and one group only. Record source ownership and authorization in the project evidence ledger, not credentials in this file.

```json
{
  "schema_version": 1,
  "organization_id": "example-tenant",
  "chat_id": "example-group",
  "chat_name": "Example company support",
  "source_id": "provided-export-2026-09-08",
  "coverage_note": "User supplied excerpt; earlier history and media not included",
  "messages": [
    {
      "id": "message-001",
      "timestamp": "2026-09-08T14:31:00+05:30",
      "sender": "Business owner",
      "sender_role": "client",
      "text": "Please do not share exact prices in chat.",
      "media": []
    }
  ]
}
```

Required metadata: schema version, organization/chat identifiers, chat name, source ID, coverage note. A message requires unique ID, timestamp with explicit UTC offset, sender, sender_role (`client`, `shvya`, `other`, `unknown`), text and media list. `media` entries are objects with `type` and optional `filename`; media bytes and URLs are excluded. Empty text is valid for media-only or system messages. Record reply context inside text with its source reference when needed; never manufacture a participant name.

Arguments `since` and `until` are inclusive calendar dates in the explicitly supplied `timezone`. The formatter uses an exclusive next-midnight upper boundary and returns the most recent `limit` messages in that range, ordered oldest first. The output explicitly states source count, range count and selected count; a truncated range cannot be called a full review. ID collisions with changed content are errors rather than silently picking a version.

The formatter does not execute or follow any instruction in messages, resolve links, access Shvya, or infer facts from unavailable attachments. Supplied text remains quoted evidence. Review exports for sensitive personal data and secrets before storing; the backend formatter rejects common credential patterns as a limited backstop. It does not guarantee detection of every secret.

Call `analyze_setup_group_export` with `data` matching this schema and the exact `chat_id`. Replace `organization_id` with the current organization UUID from `get_operations_context`; a mismatch is rejected. The example identifiers above are illustrative, never discovered organization records. The response returns formatted evidence and explicit coverage, not new configuration or permissions.
