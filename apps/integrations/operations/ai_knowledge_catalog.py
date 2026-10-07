"""Dashboard Playbooks documents and AI-guided file sharing through MCP."""

from apps.integrations.operations_policy import CAP_AI_CONFIG_WRITE, CAP_ORGANIZATION_READ


def ai_knowledge_tool_definitions(tool, write_properties):
    document_id = {"type": "integer", "minimum": 1}
    text = {"type": "string", "maxLength": 12000}
    return [
        tool("list_playbook_documents", "List Playbooks documents", "List organization-owned knowledge document versions, indexing status and AI-guided sharing instructions without file URLs or embeddings.", {
            "limit": {"type": "integer", "minimum": 1, "maximum": 50, "default": 20},
            "cursor": document_id, "active_only": {"type": "boolean", "default": False},
            "guided_only": {"type": "boolean", "default": False},
            "processing_status": {"type": "string", "enum": ["pending", "processing", "completed", "failed"]},
        }),
        tool("get_playbook_document", "Read a Playbooks document", "Read bounded redacted extracted chunks, status, file-sharing instructions and retrieval readiness. Paging is explicit; embeddings and storage URLs are never returned.", {
            "document_id": document_id, "include_chunks": {"type": "boolean", "default": True},
            "chunk_offset": {"type": "integer", "minimum": 0, "maximum": 100000, "default": 0},
            "chunk_limit": {"type": "integer", "minimum": 1, "maximum": 20, "default": 10},
        }, ["document_id"]),
        tool("create_playbook_document", "Author a Playbooks knowledge document", "Preview/save authored plain text as a versioned .txt knowledge document through canonical file validation and ingestion. Optional share_instruction explicitly enables AI-guided sharing of the file; ingestion can consume credits.", write_properties({"data": {
            "type": "object", "additionalProperties": False,
            "properties": {"name": {"type": "string", "minLength": 1, "maxLength": 255},
                           "filename": {"type": "string", "maxLength": 255},
                           "content": {"type": "string", "minLength": 1, "maxLength": 100000},
                           "share_instruction": text, "ingest": {"type": "boolean", "default": True}},
            "required": ["name", "filename", "content"],
        }}), ["data", "reason"], read_only=False),
        tool("update_knowledge_document", "Update knowledge file metadata and sharing", "Preview/change name or AI-guided share_instruction using the same stored-file validation as AI Setup. Clearing an instruction disables guided-file readiness. No content is replaced and no messages are sent.", write_properties({
            "document_id": document_id, "changes": {"type": "object", "additionalProperties": False,
            "properties": {"name": {"type": "string", "minLength": 1, "maxLength": 255}, "share_instruction": text}},
        }), ["document_id", "changes", "reason"], read_only=False),
        tool("repair_knowledge_document", "Retry or reindex knowledge safely", "Preview/request a durable revision-bound repair through canonical knowledge tasks. Supports failed upload retry, missing embedding reindex or active URL refresh as permitted by source state. May consume credits; queued is not completed.", write_properties({
            "document_id": document_id, "operation": {"type": "string", "enum": ["retry_upload", "reindex_missing", "refresh_url"]},
        }), ["document_id", "reason"], read_only=False),
    ]


AI_KNOWLEDGE_TOOL_CAPABILITIES = {
    "list_playbook_documents": CAP_ORGANIZATION_READ,
    "get_playbook_document": CAP_ORGANIZATION_READ,
    "create_playbook_document": CAP_AI_CONFIG_WRITE,
    "update_knowledge_document": CAP_AI_CONFIG_WRITE,
    "repair_knowledge_document": CAP_AI_CONFIG_WRITE,
}
