"""Stable registry for domain-oriented Operations MCP tools."""

from apps.integrations.operations.tools.cadence import (
    add_hosted_whatsapp_step,
    delete_cadence_step,
    reorder_cadence_steps,
    update_cadence_step,
)
from apps.integrations.operations.tools.faqs import archive_faq, list_faqs, upsert_faq
from apps.integrations.operations.tools.knowledge import (
    archive_knowledge_document,
    create_knowledge_source,
    publish_knowledge_document,
    upload_knowledge_document,
)
from apps.integrations.operations.tools.qualification import (
    get_qualification_configuration,
    upsert_qualification_configuration,
    validate_qualification_configuration,
)
from apps.integrations.operations.tools.simulations import (
    simulate_ai_conversation,
    simulate_cadence,
    simulate_workflow,
)
from apps.integrations.operations.tools.touchpoints import (
    archive_touchpoint,
    list_touchpoints,
    upsert_touchpoint,
)
from apps.integrations.operations.tools.whatsapp import (
    begin_whatsapp_connection,
    bind_whatsapp_account_to_pipeline,
    list_whatsapp_accounts,
    validate_whatsapp_routing,
)
from apps.integrations.operations.tools.workflows import (
    get_workflow_schema,
    list_workflow_actions,
    list_workflow_triggers,
    validate_workflow_configuration,
)


EXTENDED_HANDLERS = {
    "get_qualification_configuration": get_qualification_configuration,
    "validate_qualification_configuration": validate_qualification_configuration,
    "upsert_qualification_configuration": upsert_qualification_configuration,
    "list_whatsapp_accounts": list_whatsapp_accounts,
    "begin_whatsapp_connection": begin_whatsapp_connection,
    "bind_whatsapp_account_to_pipeline": bind_whatsapp_account_to_pipeline,
    "validate_whatsapp_routing": validate_whatsapp_routing,
    "list_workflow_triggers": list_workflow_triggers,
    "list_workflow_actions": list_workflow_actions,
    "get_workflow_schema": get_workflow_schema,
    "validate_workflow_configuration": validate_workflow_configuration,
    "list_touchpoints": list_touchpoints,
    "upsert_touchpoint": upsert_touchpoint,
    "archive_touchpoint": archive_touchpoint,
    "list_faqs": list_faqs,
    "upsert_faq": upsert_faq,
    "archive_faq": archive_faq,
    "create_knowledge_source": create_knowledge_source,
    "upload_knowledge_document": upload_knowledge_document,
    "publish_knowledge_document": publish_knowledge_document,
    "archive_knowledge_document": archive_knowledge_document,
    "add_hosted_whatsapp_step": add_hosted_whatsapp_step,
    "update_cadence_step": update_cadence_step,
    "delete_cadence_step": delete_cadence_step,
    "reorder_cadence_steps": reorder_cadence_steps,
    "simulate_ai_conversation": simulate_ai_conversation,
    "simulate_workflow": simulate_workflow,
    "simulate_cadence": simulate_cadence,
}
