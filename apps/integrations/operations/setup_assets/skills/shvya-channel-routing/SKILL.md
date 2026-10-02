---
name: shvya-channel-routing
description: Inspect and validate Shvya channel topology from lead source through account, pipeline, sender, AI eligibility and delivery path.
---

# Shvya channel routing

Own the relationship between source/channel identity and the sender that may communicate with a lead.

## Routing model

Treat routing as: source/channel -> connected account -> bound pipeline -> lead -> eligible sender/automation -> provider delivery.

## Workflow

1. Verify organization context and the exact channel involved.
2. Read `list_whatsapp_accounts`, `validate_whatsapp_routing`, messaging settings and `get_integration_lifecycle`; use integration health diagnostics for observed failures.
3. For a lead-specific issue, verify its source, current pipeline and linked account rather than choosing any connected sender.
4. Check AI/follow-up eligibility, business hours and channel-specific delivery constraints after routing is correct.
5. Use `bind_whatsapp_account_to_pipeline` only when the target relationship is explicit. Use connection/lifecycle tools only for supported providers and never request secrets through MCP.
6. Delegate provider-specific behavior to the WhatsApp, Instagram or email skill.
7. Re-run routing validation and representative trace/acceptance checks after any change.

## Guardrails

One connected account does not imply authority to message every pipeline. Do not infer a tenant from a phone number, Instagram username or provider ID. Do not treat provider connection state as proof that webhooks, templates or outbound delivery are healthy.

## Output

Return the resolved topology, mismatches, capability/settings gates, exact repair and verification.

## Shared quality contract

Before material live work, read the shared [skill quality contract](../../framework/skill-quality-contract.md), [evidence and attribution model](../../framework/evidence-and-attribution.md), [known-trap method](../../framework/known-trap-method.md), [execution and recovery contract](../../framework/execution-and-recovery.md), and this skill's [domain checks](references/domain-checks.md). Load only what the task needs, but do not report a defect before applying the relevant trap check.

Treat `evals/evals.json` as behavioral acceptance rubrics, not executed test evidence. A successful tool response is never sufficient on its own: verify authoritative read-back and the requested business effect at the strongest evidence level available. Keep UNKNOWN, conflicting and unavailable evidence explicit instead of guessing.
