# Instagram and Sandbox AI Activity

Instagram AI execution now owns a durable trace for its exact incoming message.
The trace reuses the shared generation, grounding, permission, action and provider
observations; it does not authorize an AI reply or CRM mutation. If the normal
executor creates a lead, the same observation buffer is attached to that lead
afterward. A conversation without a lead still cannot invent a CRM identity for
logging. Instagram account identity is stored as Instagram metadata, never as a
WhatsApp account. Outbound provider status updates are correlated by organization,
channel and inbound message ID. A queued response is not proof of delivery.

Sandbox turns persist separately with channel `sandbox`, a generated correlation
UUID and no lead. A database constraint permits a missing lead only for Sandbox;
live traces continue requiring a real lead. Each preview response returns its
trace ID. Sandbox trace finalization explicitly records preview execution and no
live actions. Failed previews are recorded as failures. No customer, lead,
reminder or channel message is created by tracing.

AI Activity filters include both channels and the detail view exposes already
sanitized provider-call diagnostics and available AI Brain revision metadata.
The existing signed-in organization boundary applies to the list, detail and
JSON endpoints. Existing bounded trace sanitization and credential redaction
apply at creation and flush. Trace failures do not authorize fallback actions
and do not replace a successful business result. No raw full prompts, provider
credentials or unrestricted request/response dumps are newly retained. These are
bounded execution observations, not a complete exact-prompt replay product.

Migration `0022_channel_trace_parity` changes the lead relationship only for
Sandbox's explicit schema contract and adds the Instagram/Sandbox channel choices.
PostgreSQL CI must verify migration and persistence/constraint tests before release.
Local no-database tests establish preview scope restoration, redaction and
observability-failure behavior; they cannot establish production persistence or
live channel delivery.

## Kraya comparison boundary

The supplied Kraya conversations describe its workflow and LangSmith tracing,
but explicitly state that the Kraya-LLM source was not available in that session.
This repository has no Kraya service connector, comparison runner, authorized
test account, or matched trace dataset. `evaluate_ai` is a recorded-provider
backend regression and explicitly does not benchmark a live language model.

A matched evaluation therefore still needs either an authorized Kraya test
workspace or exported complete test transcripts with organization configuration,
model/version and timings. Reuse the same business facts, Playbook, questions and
file rules in both systems, then compare pricing answers, volunteered answers,
callback priority, exact saved values, file outcomes, language, latency and model
call count. Do not substitute SHVYA unit tests for Kraya results or claim equal
quality without those paired observations.
