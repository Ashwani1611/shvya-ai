# Shvya MCP execution reference

Verified from the supplied session tool signatures and local Shvya implementation on 2026-09-24. This is a workflow-oriented map, not a promise that a future connection has every tool. Discover the current exposed schemas with MCP `tools/list` before execution; the authenticated backend registry is authoritative. The session namespace was `mcp__shvya_ai_superadmin__`; tool names below omit this host-specific prefix.

## Authorization and discovery

`get_operations_context({})` returns role, actor, active organization and effective capabilities. Superadmin selection uses `select_organization_context({organization_id, reason})` and creates a visible support session. Verify the returned organization; end that session with `clear_organization_context({reason})` when finished. Organization admins cannot select other tenants. `list_organizations` is superadmin-only discovery, not permission to operate all results.

Effective ability is the intersection of deployed/exposed tools, token grant, role, organization policy, requested scope and current resource state. Configuration-plan operations (`create_configuration_plan`, `apply_configuration_plan`, `rollback_configuration_plan`, `import_organization_configuration`) require `configuration.plan.write` and their deployed tool schemas. Use only operations exposed by the current authenticated connection. When unavailable, prepare a dependency-ordered draft; do not claim rollback or import support.

All configuration writes in this guide accept `dry_run`, a specific `reason`, and where required `approved` plus `approval_event_id`. Defaults are not an execution strategy: explicitly dry-run first. Record the immutable audit event returned by the dry-run. If approval is required, the human approves that exact proposed change, then apply the identical change with `dry_run:false`, `approved:true` and the event ID. Approval does not carry over when the target, payload or relevant state changes. Never generate an event ID or set approved preemptively.

## Read tools

| Need | Tools | Details |
|---|---|---|
| Baseline | `get_organization_configuration`, `export_organization_configuration` | Export excludes leads/messages, credentials and knowledge binaries |
| Full AI/qualification | `get_ai_configuration`, `get_qualification_configuration` | Summary excerpts are not safe full-replacement inputs; redaction flags require care |
| CRM dependencies | `get_configuration_dependency_graph`, `get_configuration_integrity_diagnostics` | Use before renaming, retiring or changing types/options |
| Automation | `get_automation_configuration({limit})`, `get_workflow_schema({trigger_type?,action_type?})`, `list_workflow_triggers`, `list_workflow_actions` | Bounded lists may require narrowed discovery; do not assume unseen entities do not exist |
| Content | `list_faqs`, `list_touchpoints`, `get_knowledge_health({limit})` | Health returns metadata, not document text or proof of correct retrieval |
| Channels | `list_whatsapp_accounts`, `get_messaging_automation_settings`, `get_integration_health`, `validate_whatsapp_routing` | Match exact pipeline and account, do not choose a random connected sender |
| Verification | `validate_organization_configuration`, `validate_qualification_configuration`, `test_ai_response_policy`, `simulate_ai_conversation`, `simulate_cadence`, `validate_workflow_configuration`, `simulate_workflow` | Read schemas and interpret limits below |

Do not assume absent specialized list/get tools exist. The current broad configuration and schema tools supply references. Request an approved export or operator action if bounded/redacted data prevents safe reconciliation.

## Configuration tools and payload fields

Common write envelope omitted below: `dry_run:true`, `reason`, followed only when required by valid approval fields on apply. IDs are UUID strings returned by this tenant, not legacy integers.

| Operation | Specific arguments | Main capability |
|---|---|---|
| Pipeline | `upsert_pipeline_configuration({pipeline_id?, data:{name?,description?,ai_enabled?,is_active?}})` | `crm.pipeline.config.write` |
| Stage | `upsert_stage_configuration({pipeline_id,stage_id?,data:{name?,description?,display_order?,ai_on?,is_active?}})` | `crm.stage.config.write` |
| Attribute | `upsert_attribute_configuration({attribute_id?,data:{name?,description?,field_type?,options?}})` | `crm.attribute.config.write` |
| AI profile | `update_ai_configuration({changes:{about?,ai_playbook?,bot_languages?,ai_enabled?,bump_up_enabled?,bump_up_count?}})` | `ai.config.write` |
| Qualification | `upsert_qualification_configuration({data:{mode?,requirements,criteria?,mappings?,target_stage_id,final_ack}})` | `ai.config.write` |
| URL knowledge | `create_knowledge_source({data:{url,name?,source_type?:"url",ingest?}})` | `ai.config.write` |
| Document | `upload_knowledge_document({data:{content_base64,filename,name?}})` then `publish_knowledge_document({document_id})` when completed/embedded | `ai.config.write` |
| FAQ | `upsert_faq({faq_id?,data:{question,answer,is_active?}})` | `ai.config.write` |
| Cadence | `upsert_cadence_configuration({cadence_id?,data:{name?,description?,provider?:"api"|"hosted",whatsapp_account_id?,is_active?}})` | `automation.cadence.config.write` |
| API template/email/reminder step | `add_cadence_step({cadence_id,data:{type,title?,template_id?,subject?,body?,text?,retry_count?,schedule?}})` | `automation.cadence.config.write` |
| Hosted free-form step | `add_hosted_whatsapp_step({cadence_id,data:{title,body,schedule?,attachment_base64?,attachment_mime_type?,attachment_name?}})` | `automation.cadence.config.write` |
| Step edit | `update_cadence_step({cadence_id,step_id,data})`; reorder through `reorder_cadence_steps` using its discovered schema | `automation.cadence.config.write` |
| Touchpoint | `upsert_touchpoint({touchpoint_id?,data:{title,body,category_id?,category_name?}})` | `automation.cadence.config.write` |
| Workflow | `upsert_workflow_configuration({workflow_id?,data})` | `automation.workflow.config.write` |
| Routing | `bind_whatsapp_account_to_pipeline({pipeline_id,whatsapp_account_id?,phone_number?,country_code?})` | `automation.messaging.config.write` |
| Hosted connection | `begin_whatsapp_connection({country_code,phone_number})` for an already bound number | `automation.messaging.config.write` |
| Meta template draft | `create_whatsapp_template({whatsapp_account_id,pipeline_id?,data:{name,body,category?,language?,footer?,buttons?}})` | `automation.messaging.config.write` |
| Meta template submit | `submit_whatsapp_template({template_id})`; batch up to 50 with `submit_whatsapp_templates({template_ids})` | `automation.messaging.config.write` |
| Messaging settings | `update_messaging_automation_settings({whatsapp_account_id,changes})` | `automation.messaging.config.write` |

Attribute types are `text`, `numeric`, `date`, `datetime`, `option`. Record the returned key separately from the display name. Do not pass legacy `dropdown`, `number`, `key`, `values` or `color_code` to tools that do not accept them. Options must remain compatible with historical values and dependencies. Stage lock flags control protected-name/deactivation behavior. New pipelines create standard stages: read these before adding more.

Messaging `changes` accepts `ai_auto_reply`, `auto_follow_up`, `auto_lead_creation`, `bump_up_count`, `bump_up_messages`, `business_hours_start`, `business_hours_end`, `active_conversation_delay_value`, `active_conversation_delay_unit` (`minutes|hours|days`). This does not expose arbitrary fixed bump-up copy, calendar settings, user invitations, billing, round robin or provider credentials. Hosted connection returns safe status; QR/session credentials are not exposed through MCP.

Meta WhatsApp template creation and submission are exposed when the authenticated connection's live `tools/list` includes `create_whatsapp_template`, `submit_whatsapp_template` and/or `submit_whatsapp_templates`. Do not redirect the operator to the dashboard login merely to create templates when these tools are present. Create customer-facing template body/footer/button text as plain text and use only placeholders supported by SHVYA's tenant-safe placeholder catalogue. Meta submission still requires the selected Meta-capable WhatsApp account and provider prerequisites; batch submission accepts up to 50 template IDs, so a set of 42 can be submitted in one batch after drafts are created.

## Actual Workflow data

Fetch `get_workflow_schema` first. The inspected backend uses:

```json
{
  "name": "Shvya - Stop outreach on opt-out",
  "enabled": false,
  "trigger_type": "keyword",
  "conditions": {
    "scopes": [{"pipeline": "{{SHVYA_PIPELINE_ID}}", "stages": ["{{SHVYA_NEW_STAGE_ID}}"]}],
    "attributes": [],
    "keywords": ["stop", "unsubscribe"]
  },
  "action_type": "stop_sequence",
  "action": {}
}
```

This is a **planning example**. Populate all relevant stages, resolve IDs, validate and obtain authorization for enablement before applying. It is not a global opt-out policy by itself: independently disable follow-up/AI where the agreed suppression model requires, and test that queued actions and other automation cannot restart outreach.

Common trigger keys: `stage_moved`, `sequence_ended`, `lead_created`, `no_response`, `keyword`, `stage_idle`, `call_logged`. Local source also contains `call_intelligence_ready`; use it only if live schema exposes it. Do not copy older documentation's fixed trigger count.

- `conditions.scopes`: array of `{pipeline, stages:[stage IDs]}`. At least one scope except `sequence_ended`, which can use empty scopes and requires `sequences:[cadence IDs]`.
- `conditions.attributes`: `{key,match:"equals"|"contains",values:[strings]}`. Conditions combine as AND; values are alternatives. Numeric less-than is not available here. Source values come from `get_workflow_schema`; optional `sources:[...]` are alternatives combined with the other conditions. A custom SOURCE field is not the built-in lead source.
- `no_response`/`stage_idle`: integer `duration` and `unit:"minutes"|"hours"|"days"`. `keyword` uses `keywords`; `*` matches any message. `call_logged` uses `call_status` from the live catalogue.
- Actions: `start_sequence` `{sequence,replace:boolean}`; `stop_sequence` `{}`; `move_stage` `{pipeline,stage}`; `ai`/`followup` `{enabled:boolean}`; `attribute` `{key,value}`; `reminder` `{duration,unit,note?,overwrite?}`; `email` `{subject,body,recipient:"lead"}`; `message` `{body,account,schedule,...}`.
- Message timing: `schedule:"relative"` with `duration,unit`; `"fixed"` with `time:"HH:MM"`; `"attribute"` with `date_attribute` referring to a real datetime attribute key. It is free-text scheduling subject to transport/window controls, **not** a `send_template_message` action. Use approved-template Cadence steps for that requirement.
- One action per Workflow. Handoff requiring stop Cadence and AI off needs coordinated rules, not an invented multi-action array. Do not assume rule order suppresses later matching rules; simulate overlapping cases.

## Updates, retirement and verification

MCP uses canonical services; legacy blanket full-replacement rules do not transfer. Read existing state, preserve unrelated fields, and apply only the accepted changes. Workflow `data` is validated as a complete definition. `changes.ai_playbook` replaces that whole string. Structured qualification upsert rewrites questions, criteria, mappings, acknowledgment and stage shifting while preserving unrelated canonical sections; it can erase additional authored routing inside replaced sections.

Archive tools exist for attributes, pipelines, stages, Cadences, Workflows, FAQs, documents and Touchpoints. Check dependencies before requested retirement. Permanent `delete_attribute`, `delete_stage` and `delete_cadence_step` have stronger constraints; step deletion rejects delivery history. Do not invent undo. Unknown outcomes require audit/read-back reconciliation before retry; sender delivery with unknown outcome is not safely replayable.

`simulate_ai_conversation({answers:{...}})` and `test_ai_response_policy({answers?})` are deterministic qualification/policy checks against the **stored** Playbook, not LLM text-generation tests. `simulate_workflow({lead_id,workflow_id? or data?,event?})` evaluates a real tenant lead with synthetic event data, without executing actions. `simulate_cadence({cadence_id,reference_at?})` calculates timing without enrollment or sends. None creates a lead. Report unavailable test fixtures and real provider verification honestly.
