---
name: shvya-ai-playbook
description: Author, review and safely publish Shvya's canonical eight-section AI Playbook without overwriting unrelated AI Brain state.
---

# Shvya AI Playbook

Own customer-facing AI behavior: persona, response policy, qualification conversation, mappings/actions, handoff, confidentiality and channel-specific instructions.

## Workflow

1. Read the complete current Playbook with `get_ai_configuration`; preserve unrelated authored behavior.
2. Read CRM attributes/stages, qualification configuration, FAQs/knowledge metadata and current messaging constraints needed by the requested change.
3. Use the canonical eight-section structure and required parser markers. Keep customer-visible copy inside the supported message tags and private operating instructions outside them.
4. Use real attribute names/options and real stage semantics. A Playbook cannot create CRM fields, send messages, move stages or create reminders by declaration alone.
5. Keep factual product/pricing/policy content in About/FAQ/knowledge when appropriate instead of turning the Playbook into a duplicate knowledge base.
6. Validate the draft with the canonical template/qualification compiler where available, then use `update_ai_configuration(changes.ai_playbook)` with dry-run and approval.
7. Read back the full saved Playbook. Run policy and conversation simulations after publication.

## Guardrails

Never publish unresolved `{{SHVYA_*}}` authoring variables, hard-coded recipient names, secrets, unsupported runtime placeholders or instructions that conflict with backend consent/routing rules.

## Output

Return the sections changed, preserved behavior, dependency assumptions, validation results, read-back confirmation and any separate CRM/knowledge action still required.

## Shared quality contract

Before material live work, read the shared [skill quality contract](../../framework/skill-quality-contract.md), [evidence and attribution model](../../framework/evidence-and-attribution.md), [known-trap method](../../framework/known-trap-method.md), [execution and recovery contract](../../framework/execution-and-recovery.md), and this skill's [domain checks](references/domain-checks.md). Load only what the task needs, but do not report a defect before applying the relevant trap check.

Treat `evals/evals.json` as behavioral acceptance rubrics, not executed test evidence. A successful tool response is never sufficient on its own: verify authoritative read-back and the requested business effect at the strongest evidence level available. Keep UNKNOWN, conflicting and unavailable evidence explicit instead of guessing.
