---
name: shvya-ai-debugger
description: Trace Shvya AI response failures from inbound message through lead resolution, permissions, queue, prompt/knowledge, actions, outbound queue and provider delivery.
---

# Shvya AI debugger

Use when AI is silent, inaccurate, repetitive, not grounded, not saving attributes/stages/reminders, or behaves differently across channels.

## Diagnostic chain

Trace the request in order:

1. Inbound/webhook evidence and lead/conversation resolution.
2. Correct organization, pipeline, channel account and routing.
3. Universal AI Auto-Reply, stage AI state, lead AI state, business hours and active-conversation delay.
4. Queue/job creation and runtime worker health.
5. Prompt/Playbook assembly and qualification state.
6. Knowledge retrieval/grounding evidence.
7. Model/provider response.
8. Parsed actions: attributes, stage, reminders, files, handoff.
9. Outbound message creation/queue claim.
10. Provider acceptance and delivery status.

Use `get_lead_snapshot`, `get_conversation`, `trace_message`, `get_ai_diagnostics`, `get_integration_health`, `get_recent_errors`, `get_runtime_health`, `get_workflow_trace` and `get_production_trace` as available.

## Repair rule

Identify the producing layer before editing. A poor answer caused by missing retrieval is not fixed by adding more fallback text. A blocked action caused by CRM validation is not fixed by prompt wording. Hand the repair to the narrowest domain skill, then rerun the same trace/acceptance scenario.

## Output

Return a layer-by-layer trace with PASS/FAIL/UNKNOWN, the first verified failure, supporting IDs/timestamps, exact repair owner and post-fix verification. Do not expose raw secrets, hidden reasoning or unnecessary conversation content.
