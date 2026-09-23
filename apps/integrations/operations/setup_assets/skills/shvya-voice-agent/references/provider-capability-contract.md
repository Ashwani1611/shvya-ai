# Voice integration capability gap and adapter handoff

The captured Shvya MCP has no tools for listing/provisioning voice agents, publishing provider versions, placing calls, fetching voice recordings/transcripts or applying post-call outcomes. `get_conversation` covers WhatsApp/Instagram lead messages; `trace_message` traces their processing. Neither is a call transcript tool. `simulate_ai_conversation` checks qualification deterministically and does not synthesize speech or test a voice provider.

A future integration needs the following reviewed capabilities before the artifact can operate:

| Capability | Contract to implement and verify |
|---|---|
| Tenant binding | Authenticated organization scope, tenant-owned agent/lead checks, no prompt-selected organization |
| Secrets | Server-held provider credentials and webhook verification; never ask for API keys in a prompt or chat |
| Read agent version | Agent identity, current published version, provider system constraints, language/voice configuration; safe redaction |
| Draft/write/publish | Typed fields, exact dry-run diff, authorization and any required approval, read-back, last-known-good version and rollback procedure |
| Call launch | Explicit authorized recipient and purpose, consent/contact restrictions, suppression and timing checks, spend bounds; isolated test recipients first |
| Runtime context | Verified mapping of call instructions and safe lead facts into provider inputs, no unresolved template values |
| Tools | Actual end-call, human handoff and supported action-result contracts; instructions alone do not install functions |
| Call evidence | Scoped ID lookup, status, timestamp/timezone, transcript provenance, restricted recording access and coverage notes |
| Outcome processing | Signed callback, organization/lead binding, canonical qualification engine, typed evidence, idempotency, audit; no transcript-triggered direct writes |
| Operations | Redacted errors, queue/retry visibility, duplicate suppression, rollback and owner |

This checklist is a proposed integration contract, not a set of new tool names or working endpoints. Provider-specific API facts must be checked in the selected provider's official current documentation during implementation. Do not reuse source-repo URLs, workspace IDs, model/language IDs, price assumptions, database tables or credential decryption recipes.

Source observations such as “changing voice forces prompt publication”, “latest published version wins”, provider transcript omission, stereo channel assignment, language overrides and ASCII keyword limits remain debugging hypotheses for that provider/version only. Reproduce and confirm before applying them. No voice swap or republish is a universal publishing requirement.

No replacement REST helper is shipped because none could truthfully use the exposed Shvya MCP for voice. The useful replacement is this reviewed adapter contract, prompt pair and test matrix; that preserves the workflow without bypassing Allowed capabilities.
