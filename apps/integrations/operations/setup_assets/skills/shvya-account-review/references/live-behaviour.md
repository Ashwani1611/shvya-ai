# Bounded review of real behavior

Current native tools provide persisted messaging and sanitized diagnostics, not a general raw model-trace population API. Do not carry over a legacy LangSmith project ID, retention assumption, SQL table or credential lookup.

## Choose and state the cohort

Start with the user's concrete incident identifiers. Resolve a person/phone/email through `find_leads` and inspect ambiguity before choosing a lead. Use `find_affected_leads` for a specific persisted issue signal (qualification completion mismatch, Workflow/AI/delivery failure or stalled stage). Combine with `get_conversion_analysis` and `get_runtime_health` for returned aggregate context.

Record selection method, limits, period, timezone and returned count. Deduplicate lead IDs across issue cohorts. This is an incident-focused sample, not a representative random sample. Where tools do not offer pagination, do not invent cursors or repeatedly widen queries and claim exhaustiveness. An org-wide rate needs a genuine denominator from compatible aggregate evidence.

For each selected lead use `get_lead_snapshot`, `get_conversation` and the relevant message/Workflow/qualification diagnostic. Store source IDs and tool read timestamps. A conversation is recent and bounded; it is not guaranteed lifetime history. Distinguish message content, generated output, enqueue success, provider acceptance and confirmed delivery/read when those states are available.

## Probes and attribution

- Links: compare exact sent strings to approved current and incident-time references; do not manufacture slugs or infer delivery from a clickable-looking URL.
- Commitments: distinguish asking a preferred time from confirming a booked slot, callback or named staff availability. Seek the actual scheduling/dispatch result before treating the commitment as fulfilled.
- Figures and policies: search About, full available Playbook, FAQs, Cadences and Touchpoints for the exact number/claim and context. If found, identify the conflicting source. If absent, do not conclude hallucination while document content or historical runtime prompt is unavailable; report an unsupported claim with attribution unresolved.
- Unsupported offering: compare the precise affirmative reply to the approved business scope and exclusions. A question about a service is not confirmation it is offered.
- Repetition: distinguish duplicate delivery of the same message, duplicate generation, legitimate reminder and a human reply. Normalize conservatively; preserve message IDs and timing.
- Handoff/opt-out: trace whether continuing messages came from AI auto-reply, a Cadence, Workflow or human operator. A correct qualification decision can coexist with a follow-up path that never stopped.
- Human/AI overlap: use explicit available sender/automation metadata. Missing from a model-output set does not prove a human wrote a message; attribution may be unavailable.
- Timing: use offset-aware timestamps and comparable definitions. Pair inbound and first eligible outbound only when both are visible; identify bot versus all-response latency. Split business/outside hours only with known schedule/timezone. Bounded recent conversations can censor the next reply, so report those cases separately.
- Errors: count attempts and distinct leads separately, then check later success or delivery for each affected lead. A retry storm that recovered differs from unreplied customers. Do not assume all errors are harmless; do not infer wasted spend without cost evidence.

## Evidence contract for a finding

Record ID, requirement, exact source quotation/message ID, observed period, affected distinct leads in sample, compatible denominator if known, current configuration, incident-time uncertainty, attribution layer/confidence, impact and owner. Native counters, simulations and message traces each prove only their documented scope. If raw call recordings, historical prompts, document text or provider logs are needed but absent, say exactly which question remains unanswered.
