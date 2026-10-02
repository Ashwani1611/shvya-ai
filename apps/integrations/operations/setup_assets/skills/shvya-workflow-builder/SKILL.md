---
name: shvya-workflow-builder
description: Design, validate, simulate and safely apply Shvya Workflows using live trigger/action schemas and tenant-owned dependencies.
---

# Shvya Workflow builder

Own event-condition-action automation. Use this skill when creating or editing Workflows or when the desired business process should be expressed as a Workflow.

## Workflow

1. Verify organization context and read `get_workflow_schema`, `list_workflow_triggers`, `list_workflow_actions`, CRM configuration and relevant channel/Cadence dependencies.
2. Translate the business requirement into one trigger, explicit scopes/conditions and one supported action. Do not invent multi-action semantics when the backend supports one action per Workflow.
3. Resolve real pipeline, stage, attribute, Cadence and sender IDs. Source conditions must use the live schema; custom Source attributes are not the built-in lead source.
4. Keep newly authored Workflows disabled until validation, overlap review and downstream resources are ready.
5. Run `validate_workflow_configuration` and `simulate_workflow` against representative tenant leads/events.
6. Check overlap with existing Workflows, loops, conflicting stage moves, AI/follow-up re-enablement and opt-out/handoff suppression.
7. Use `upsert_workflow_configuration` with dry-run/approval/read-back. Activate only when the requested scope includes activation and the dependencies are verified.

## Guardrails

Do not rely on rule order as a suppression guarantee. Do not use a Workflow to force Qualified outside the qualification contract. Do not schedule provider-unsupported free-text sends.

## Output

Return trigger, conditions, action, bound IDs, conflict analysis, simulations, activation state and post-write read-back.
