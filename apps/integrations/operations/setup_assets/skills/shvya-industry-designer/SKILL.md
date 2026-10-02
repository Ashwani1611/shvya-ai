---
name: shvya-industry-designer
description: Translate a company's industry, sales motion and operating constraints into a Shvya configuration blueprint before CRM, AI or automation is changed.
---

# Shvya industry designer

Use this skill when the user wants a recommended Shvya setup for a business type, wants to adapt an industry playbook, or needs a configuration blueprint before live changes.

## Operating contract

Start with `get_operations_context` for live work. Use `list_industry_playbooks` only as reusable patterns, never as company facts. Read the target organization's actual business description, CRM, AI, qualification, channels and current configuration before proposing changes. Industry examples do not authorize writes and must not overwrite existing tenant-specific decisions.

## Workflow

1. Identify the business model, lead sources, products/services, sales cycle, qualification gate, human handoff points, languages, channels, appointment needs and compliance constraints.
2. Inspect the current organization with `get_organization_configuration`, `get_ai_configuration`, `get_qualification_configuration`, `get_automation_configuration`, `get_messaging_automation_settings` and available integration/calendar reads.
3. Use `list_industry_playbooks` as a starting pattern. Mark every item as reuse, adapt, create, or defer.
4. Produce a dependency-ordered blueprint covering pipelines, stages, attributes, qualification, AI Brain, knowledge, Workflows, Cadences, Touchpoints, routing, Calendar and team handoff.
5. Delegate execution to the relevant domain skill. Do not apply a large cross-domain setup directly when a narrower skill owns that domain.
6. Finish with acceptance criteria that can be checked by `run_acceptance_suite`, simulations, routing validation and read-back.

## Output

Return the business assumptions, current-state evidence, target blueprint, dependency order, unsupported requirements, and the specific domain skills required next. Never claim an industry template is a proven best practice for the customer without evidence from that customer's process.

## Shared quality contract

Before material live work, read the shared [skill quality contract](../../framework/skill-quality-contract.md), [evidence and attribution model](../../framework/evidence-and-attribution.md), [known-trap method](../../framework/known-trap-method.md), [execution and recovery contract](../../framework/execution-and-recovery.md), and this skill's [domain checks](references/domain-checks.md). Load only what the task needs, but do not report a defect before applying the relevant trap check.

Treat `evals/evals.json` as behavioral acceptance rubrics, not executed test evidence. A successful tool response is never sufficient on its own: verify authoritative read-back and the requested business effect at the strongest evidence level available. Keep UNKNOWN, conflicting and unavailable evidence explicit instead of guessing.
