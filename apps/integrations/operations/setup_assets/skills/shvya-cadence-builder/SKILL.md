---
name: shvya-cadence-builder
description: Design, build, edit and validate Shvya Cadences across WhatsApp templates, Hosted WhatsApp, email and reminders with correct timing and routing.
---

# Shvya Cadence builder

Own ordered follow-up sequences and their delivery prerequisites.

## Workflow

1. Verify context. Read existing Cadences, the target pipeline/account routing, messaging settings and available WhatsApp templates.
2. Decide provider/sender first. Existing Cadence sender/provider cannot be changed in place; create a new Cadence when the sender/provider must change.
3. Create/reuse the Cadence with `data.is_active:true`, but keep unfinished Cadences isolated from enrollment and enabled Workflows.
4. Add supported steps using `add_cadence_step` or `add_hosted_whatsapp_step`. Preserve approved template requirements, valid variables, attachment rules and schedule semantics.
5. Use `update_cadence_step`, `reorder_cadence_steps` and `delete_cadence_step` only after inspecting delivery history and current order.
6. Run `validate_cadence_batch` for planned batches and `simulate_cadence` for timing. Check business hours, opt-out/handoff exits, duplicate follow-up risk and pipeline routing.
7. Apply through dry-run/approval/read-back and verify the final contiguous order and active state.

## Customer-copy formatting

Before writing steps or related Touchpoints, read [customer-facing content gates](../../framework/customer-content-gates.md). Use plain-text copy, one useful purpose/CTA and natural first-name personalization without repeated greetings. Preview populated/missing names, links, options and any caption on the selected provider. Preserve approved-template text/mappings rather than silently converting a template to free text. Formatting validation does not authorize sends, enrollment, activation or timing changes; report untested delivery surfaces as UNKNOWN.

## Guardrails

Do not send test messages just because a Cadence was configured. API free text remains subject to provider service-window rules. Do not silently convert an API template step into Hosted free-form copy.

## Output

Return Cadence ID, sender/provider, ordered steps, timing, validation/simulation results, enrollment dependencies and remaining provider readiness gaps.

## Shared quality contract

Before material live work, read the shared [skill quality contract](../../framework/skill-quality-contract.md), [evidence and attribution model](../../framework/evidence-and-attribution.md), [known-trap method](../../framework/known-trap-method.md), [execution and recovery contract](../../framework/execution-and-recovery.md), and this skill's [domain checks](references/domain-checks.md). Load only what the task needs, but do not report a defect before applying the relevant trap check.

Treat `evals/evals.json` as behavioral acceptance rubrics, not executed test evidence. A successful tool response is never sufficient on its own: verify authoritative read-back and the requested business effect at the strongest evidence level available. Keep UNKNOWN, conflicting and unavailable evidence explicit instead of guessing.
