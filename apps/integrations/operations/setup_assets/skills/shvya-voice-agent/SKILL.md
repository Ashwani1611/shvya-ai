---
name: shvya-voice-agent
description: Prepare and review Shvya voice-agent prompts, call-flow instructions, pronunciation guidance, outcome contracts and test plans. Current Shvya MCP does not expose voice provisioning, calling, voice-version publication or call-recording retrieval; produce integration-ready artifacts and an explicit handoff.
---

# Shvya voice-agent preparation

This skill adapts voice design and debugging lessons into reusable Shvya artifacts. It does not claim the connected MCP can create or call a voice agent. Use [provider-capability-contract.md](references/provider-capability-contract.md) to identify the precise integration gap. A Workflow schema that lists an action is not automatically permission to provision a voice provider; a generic Cadence data object does not prove `ai_call` support.

Read the [agent prompt](references/agent-prompt-template.md) and [call instructions](references/call-instructions-template.md) before drafting. Use Shvya compile-time tokens from the kit variable registry; they are not native voice dynamic variables. Unknown company facts stay unresolved in drafts and must never reach a live script as placeholders.

Keep three layers distinct:

| Layer | Owns | Current status |
|---|---|---|
| Agent policy | Identity, speaking style, disclosure, guardrails, repair, closing behavior | Draft returned by `render_setup_template` |
| Call instructions | Objective, ordered questions, approved business context, branch logic and outcome descriptions | Draft returned by `render_setup_template` |
| Lead context | Known current facts, provenance, consent/contact status and reliable identifiers | Minimized provider adapter input to be implemented; do not invent native injection |

Place facts once in an approved knowledge/context block and reference them elsewhere. Conflicting provider system prompts, duplicate price rules and incompatible language directives are design defects. Inspect the actual provider's documented runtime behavior when an integration is selected; old provider observations are not universal guarantees.

For English/Hindi deployments, pair important spoken examples, write Hindi in Devanagari, expand abbreviations and maintain a verified pronunciation dictionary for names/acronyms. Spell telephone numbers as spoken digits; monetary amounts use normal language and units, including appropriate Hindi amounts. Do not hardcode a provider's voice ID, model ID, price, pause syntax, keyword limit or language ID. Use the company's configured languages; bilingual behavior is not a default for every company.

Keep turns short, ask one question at a time, avoid acknowledgment loops, accept mapped short answers and respect refusal. Do not re-ask reliable known information; stale, contradictory or placeholder context is not reliable. Ask for clarification on material ambiguity instead of taking a guess. Do not promise a scheduled callback, sent link, payment, booking, CRM stage update or opt-out persistence unless the corresponding backend result confirms it.

Before eventual deployment, require the adapter to support a real terminal action and verified opt-out/handoff behavior. Writing “end the call” does not create an end-call function. Separate preliminary caller interest from canonical Shvya qualification completion. Business eligibility decisions remain governed by the approved Playbook and backend, including required answers and branch applicability.

Use [testing-and-debugging.md](references/testing-and-debugging.md) for scenario tests, trace limitations, speech diagnostics and version checks. Use `render_setup_template` with `voice-agent` and `voice-call-instructions` for deterministic draft preparation. Current voice work is artifact-only: no calls, publishing, provider API writes, test leads, external sends, credential retrieval or voice swaps. Deliver the two prompts, unresolved facts, adapter capability checklist and local test cases. Do not mark voice integration operational until an authorized implementation and end-to-end evidence exist.

## Shared quality contract

Before material live work, read the shared [skill quality contract](../../framework/skill-quality-contract.md), [evidence and attribution model](../../framework/evidence-and-attribution.md), [known-trap method](../../framework/known-trap-method.md), [execution and recovery contract](../../framework/execution-and-recovery.md), and this skill's [domain checks](references/domain-checks.md). Load only what the task needs, but do not report a defect before applying the relevant trap check.

Treat `evals/evals.json` as behavioral acceptance rubrics, not executed test evidence. A successful tool response is never sufficient on its own: verify authoritative read-back and the requested business effect at the strongest evidence level available. Keep UNKNOWN, conflicting and unavailable evidence explicit instead of guessing.
