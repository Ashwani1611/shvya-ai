"""Compatibility facade for the domain-oriented Operations MCP tools.

Keep imports from this historical module working while implementation lives in
apps.integrations.operations.tools.
"""

from apps.integrations.operations.constants import MAX_MCP_KNOWLEDGE_UPLOAD_BYTES
from apps.integrations.operations.registry import EXTENDED_HANDLERS
from apps.integrations.operations.tools.cadence import (
    _attachment_from_data,
    _cadence,
    _step_snapshot,
    add_hosted_whatsapp_step,
    delete_cadence_step,
    reorder_cadence_steps,
    update_cadence_step,
)
from apps.integrations.operations.tools.faqs import (
    _faq_id,
    _faq_proposal,
    archive_faq,
    list_faqs,
    upsert_faq,
)
from apps.integrations.operations.tools.knowledge import (
    _decode_upload,
    _knowledge_source,
    archive_knowledge_document,
    create_knowledge_source,
    publish_knowledge_document,
    upload_knowledge_document,
)
from apps.integrations.operations.tools.qualification import (
    _qualification_public_snapshot,
    _safe_requirements_for_org,
    get_qualification_configuration,
    upsert_qualification_configuration,
    validate_qualification_configuration,
)
from apps.integrations.operations.tools.simulations import (
    _resolve_simulated_answer,
    simulate_ai_conversation,
    simulate_cadence,
    simulate_workflow,
)
from apps.integrations.operations.tools.touchpoints import (
    _touchpoint_proposal,
    archive_touchpoint,
    list_touchpoints,
    upsert_touchpoint,
)
from apps.integrations.operations.tools.whatsapp import (
    _routing_snapshot,
    begin_whatsapp_connection,
    bind_whatsapp_account_to_pipeline,
    create_whatsapp_template,
    get_whatsapp_template_status,
    list_whatsapp_accounts,
    list_whatsapp_templates,
    submit_whatsapp_template,
    submit_whatsapp_templates,
    validate_whatsapp_routing,
)
from apps.integrations.operations.tools.workflows import (
    _action_schema,
    _trigger_schema,
    _workflow_catalog_safe,
    get_workflow_schema,
    list_workflow_actions,
    list_workflow_triggers,
    validate_workflow_configuration,
)


__all__ = [
    "EXTENDED_HANDLERS",
    "MAX_MCP_KNOWLEDGE_UPLOAD_BYTES",
    "_action_schema",
    "_attachment_from_data",
    "_cadence",
    "_decode_upload",
    "_faq_id",
    "_faq_proposal",
    "_knowledge_source",
    "_qualification_public_snapshot",
    "_resolve_simulated_answer",
    "_routing_snapshot",
    "_safe_requirements_for_org",
    "_step_snapshot",
    "_touchpoint_proposal",
    "_trigger_schema",
    "_workflow_catalog_safe",
    "get_qualification_configuration",
    "validate_qualification_configuration",
    "upsert_qualification_configuration",
    "list_whatsapp_accounts",
    "list_whatsapp_templates",
    "get_whatsapp_template_status",
    "create_whatsapp_template",
    "submit_whatsapp_template",
    "submit_whatsapp_templates",
    "begin_whatsapp_connection",
    "bind_whatsapp_account_to_pipeline",
    "validate_whatsapp_routing",
    "list_workflow_triggers",
    "list_workflow_actions",
    "get_workflow_schema",
    "validate_workflow_configuration",
    "list_touchpoints",
    "upsert_touchpoint",
    "archive_touchpoint",
    "list_faqs",
    "upsert_faq",
    "archive_faq",
    "create_knowledge_source",
    "upload_knowledge_document",
    "publish_knowledge_document",
    "archive_knowledge_document",
    "add_hosted_whatsapp_step",
    "update_cadence_step",
    "delete_cadence_step",
    "reorder_cadence_steps",
    "simulate_ai_conversation",
    "simulate_workflow",
    "simulate_cadence",
]
