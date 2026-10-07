"""Native client Vault tools. Local setup intake is a separate evidence store."""

CAP_VAULT_READ = "vault.read"
CAP_VAULT_WRITE = "vault.write"


def vault_tool_definitions(tool, write_properties):
    string = {"type": "string"}
    uuid = {"type": "string", "format": "uuid"}
    section = {"type": "string", "enum": ["website", "brochures", "media", "offerings", "basics", "faqs", "team", "qualification", "handoff", "blacklist", "rules", "proof", "offers", "scripts", "other"]}
    entry = {
        "section": section, "kind": {"type": "string", "enum": ["note", "link", "file", "audio"]},
        "external_id": {"type": "string", "minLength": 1, "maxLength": 200},
        "body": {"type": "string", "maxLength": 20000}, "url": {"type": "string", "maxLength": 2048},
        "origin": {"type": "string", "enum": ["fireflies", "whatsapp", "ops_chat", "other"]},
        "source_date": {"type": ["string", "null"]},
        "send_when": {"type": "string", "maxLength": 4000}, "allowed_for_ai_sharing": {"type": "boolean"},
    }
    def data(props, required):
        return write_properties({"data": {"type": "object", "properties": props, "required": required, "additionalProperties": False}})

    return [
        tool("get_vault", "Inspect native client Vault", "Read this organization's real client-facing Vault status, 15 sections, counts and storage; excludes client links, access codes and tokens."),
        tool("export_vault", "Export client Vault evidence", "Read a paginated evidence export from the native Vault. Client edits take priority; nothing is published to live AI Setup. Follow next_cursor for each collection.", {
            "collection": {"type": "string", "enum": ["entries", "questions", "calls"], "default": "entries"}, "section": section,
            "limit": {"type": "integer", "minimum": 1, "maximum": 50, "default": 20}, "cursor": uuid,
        }),
        tool("get_vault_entry", "Read a Vault entry", "Read the full bounded entry, client override, provenance and asset metadata within this organization's Vault.", {"entry_id": uuid}, ["entry_id"]),
        tool("get_vault_asset", "Inspect a private Vault asset", "Read private asset metadata and an optional redacted UTF-8 preview for TXT, MD or CSV. Binary assets require the authenticated Vault UI; no signed URL is exposed.", {"entry_id": uuid, "include_text": {"type": "boolean", "default": False}}, ["entry_id"]),
        tool("create_vault_workspace", "Create native client Vault", "Superadmin only. Preview/create this organization's actual Vault. Access credentials are managed in the authenticated Superadmin UI and never returned over MCP.", write_properties({"name": {"type": "string", "maxLength": 255}}), ["reason"], read_only=False),
        tool("upsert_vault_entry", "Record a client-visible Vault fact", "Preview/create/update an agent-owned note/link by stable external_id. Maximum 300 words; client edits and confirmations cannot be overwritten. Entries are visible in the client's Vault.", data(entry, ["section", "external_id"]), ["data", "reason"], read_only=False),
        tool("upsert_vault_question", "Ask a client-visible Vault question", "Preview/create/update an agent-owned question by stable external_id. Client answers are protected; answered questions cannot be rewritten.", data({"section": section, "text": {"type": "string", "maxLength": 2000}, "external_id": entry["external_id"]}, ["text", "external_id"]), ["data", "reason"], read_only=False),
        tool("upsert_vault_call", "Record a Vault call", "Preview/create/update a client-visible call summary by stable external_id. Recording visibility is explicit; no AI configuration changes.", data({"title": {"type": "string", "maxLength": 200}, "date": string, "url": entry["url"], "summary": {"type": "string", "maxLength": 10000}, "duration_min": {"type": ["integer", "null"], "minimum": 0, "maximum": 10080}, "attendees": {"type": "array", "maxItems": 100, "items": {"type": "string", "maxLength": 200}}, "external_id": entry["external_id"], "share_recording": {"type": "boolean", "default": False}}, ["title", "date", "external_id"]), ["data", "reason"], read_only=False),
        tool("upload_vault_asset", "Upload a private client Vault asset", "Preview/upload an agent-owned file/audio up to 512 KiB using native encrypted storage, content validation and quota. Content digest binds approval; no knowledge publication or sending occurs.", data({**entry, "filename": {"type": "string", "maxLength": 200}, "content_base64": {"type": "string", "maxLength": 699052}}, ["section", "external_id", "filename", "content_base64"]), ["data", "reason"], read_only=False),
        tool("set_vault_section", "Update Vault section progress", "Preview/update a real Vault section's state and completion. Cannot mark material missing when content exists; records team action without impersonating client confirmation.", write_properties({"section": section, "state": {"type": "string", "enum": ["empty", "filled", "dont_have"]}, "is_done": {"type": "boolean"}}), ["section", "reason"], read_only=False),
    ]


VAULT_TOOL_CAPABILITIES = {
    **dict.fromkeys(("get_vault", "export_vault", "get_vault_entry", "get_vault_asset"), CAP_VAULT_READ),
    **dict.fromkeys(("create_vault_workspace", "upsert_vault_entry", "upsert_vault_question", "upsert_vault_call", "upload_vault_asset", "set_vault_section"), CAP_VAULT_WRITE),
}
