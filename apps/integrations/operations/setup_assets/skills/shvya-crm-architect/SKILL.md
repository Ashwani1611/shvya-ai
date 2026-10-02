---
name: shvya-crm-architect
description: Design, validate and safely change Shvya pipelines, stages and custom attributes while preserving qualification and automation dependencies.
---

# Shvya CRM architect

Own CRM structure: pipelines, stages, stage descriptions, stage AI state, custom attributes, option sets and dependency-safe retirement.

## Workflow

1. Verify organization context and read `get_organization_configuration`, `get_qualification_configuration`, `get_automation_configuration` and `get_configuration_dependency_graph`.
2. Model the real sales process before changing labels. Reuse equivalent records instead of duplicating them.
3. Keep protected Shvya stages intact. Qualification completion remains owned by the qualification contract.
4. For attributes, use only canonical types: `text`, `numeric`, `date`, `datetime`, `option`. Preserve historical option compatibility and never infer keys from display names.
5. Before a stage or attribute change, inspect dependent qualification mappings, Workflows, Cadences and required-field rules.
6. Use `upsert_pipeline_configuration`, `upsert_stage_configuration`, `upsert_attribute_configuration` and `reorder_stages` with dry-run first. Use archive before delete unless permanent deletion is explicitly required and dependency checks pass.
7. Re-read the changed configuration and run `validate_organization_configuration` plus integrity diagnostics.

## Guardrails

Never move leads as part of schema design. Never rename/deactivate protected stages to force a desired process. Never create duplicate attributes when a semantically equivalent field exists. Do not change an established attribute type without explicit migration intent.

## Output

Report reused/created/changed IDs, dependency effects, validation results, unresolved migration risks and any required qualification/Workflow updates.
