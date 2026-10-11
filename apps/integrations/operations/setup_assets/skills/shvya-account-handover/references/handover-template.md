# Account handover template

Produce an operational explanation, not an inventory dump. Default to a matched Markdown file and a standalone printable HTML file from one document model. Use only the selected organization's facts. The eight navigation sections below are the reader-facing structure; the source ledger is an appendix. Every material claim links to a source ID. Unknown is not the same as absent.

## Header and operating overview (overview)

Title: "<Organization>: how the account works".

Identify exact organization, evidence capture time and timezone, environment and audience. Explain that configuration-based expectations are not observed lead behavior. List existing engine and delivery test evidence separately; no tests run during generation.

Write a compact account-specific narrative covering:

- Inbound sources, pipelines, real sender connections and actual owner(s).
- Qualification objective, required facts and completion destination.
- Automatic routing, follow-up path, manual handoff and outcomes.
- The team's responsibilities, alerts and assignment behavior.
- Immediate blockers or material contradictions, each linked to an open question.

Lead with how the business operates. Put exhaustive IDs in the evidence appendix or relevant tables. Do not imply a channel is connected merely because its intended number is configured.

## Lead flow (flow)

One graph per actual pipeline. Label entry, every relevant stage, business meaning, AI permission, ownership and each transition's condition.

- Automatic: solid; backed by a saved rule or supported compiled transition.
- Manual: dashed; a documented action the team performs.
- Requested/unconfigured/refused: dotted or explicitly marked; never drawn as automatic.

Keep names exact and distinguish similarly named stages by pipeline and ID in evidence. Do not derive edges from stage order. Explain omitted repeated edges, suppression on stage entry and any node with no verified exit. Effective readiness also depends on organization/user/lead/channel/credits/schedule gates, not only stage color. Provide a textual transition equivalent for accessibility and standalone HTML.

## AI qualification and knowledge (qualification)

Include these tables with real configured rows:

1. Question order; exact question or faithful summary; required/optional; attribute/key filled; skip condition; source.
2. Completion gate; final acknowledgement; allowed destination; exceptions; source.
3. Routing destination; triggering evidence/condition; priority/conflict; actual executable authority; source.
4. Always/never policies: language, tone, answer-before-question, repetition, pricing/disclosure, human request and opt-out.
5. Captured-versus-derived attributes, descriptions and extraction rules.
6. Files: title/ID, what triggers sharing, once/repeat rule, actual published/share permission and delivery limitations.
7. FAQ, indexed document and Quick Reply coverage: categories, quantities, contradictions, source completeness.

Do not quote invented AI transcripts. Distinguish requested prompt behavior from compiled backend eligibility and verified traces. If a feature is not configured or cannot be inspected, name that state explicitly.

## Workflows, Cadences and feature boundaries (automation)

Workflow table: execution priority/order; name and ID; active/disabled; event; full pipeline/stage/attribute/time conditions; ordered actions; sender/owner/destination; replace/overwrite behavior; evidence.

Cadence overview: name/ID; purpose; active/enrollable state; channel and real sender; start rule; schedule window and timezone; reply pause; stop/restart policy; completion action.

For every Cadence provide its full ordered step table: step ID/type; delay unit and anchor; delay since previous step; cumulative offset since enrollment; message or approved template summary; attachment and variable binding; evidence. Say when a step is explain-only during a demo. Distinguish configured delays from delivery times. Read full typed details before deciding a Cadence is empty.

Check competing timers, STOP re-enrollment, human/closed-stage behavior, final-message/completion races and inactive seeded content. List unsupported or not-configured Calendar, catalog, voice, templates and other features honestly. Where configured, include Calendar page/public link readiness, ownership, lead mapping, timezone, reminders and actual booking-versus-request distinction.

## Demonstration (demo)

Start with preflight readiness, exact approved fixture identities or an explicit missing-target note, initial states, running order and what is explained instead of executed.

Every step uses:
**Send / Do / Explain only → Expect → Dashboard → Why → Say → Evidence**.

Expect is configured behavior unless a separately identified result proves it. Dashboard identifies observable saved values, stage, assignment, reminder, enrollment or suppression. Why teaches the operator; Say is a short presenter line that does not overpromise.

Cover applicable happy path, compound/early answers, language variation, knowledge question, alternate routing, non-fit enquiry, human handoff, STOP, each important Cadence start/stop/completion, and configured Calendar/catalog/voice. Unsupported scenarios go to Not covered. Include real gaps in the script instead of pretending a disconnected flow works.

Include before/after-call instructions: safety checks, which approved fixture cleanup is needed, and how to record client requests. Generating the script never connects accounts, enrolls Cadences, creates/moves/deletes leads or sends messages. Do not propose bypassing a quiet human-owned stage with an immediate campaign solely to demonstrate it.

## Day-to-day operations (day_to_day)

For each human-owned state: who checks it, what alert exists, intended response interval only if sourced, what to read on the card, exact manual action and next stage. Distinguish an attribute desk label from a real user assignment.

Describe monitoring, sender/credit health and escalation. Explain persistent lead-level AI-off and how an authorized operator handles resumption. Include approved commitments with sourced owners/deadlines; otherwise "unassigned" or "not agreed". Avoid blanket instructions such as "never disable AI" that prevent safe incident response.

## Open questions (open_questions)

One concrete finding per entry:

- Severity and title.
- Observed state with exact source and coverage.
- Consequence or reachable conflict path.
- What remains uncertain.
- Decision needed and responsible owner, if known.

Order launch/safety blockers before improvements. Re-read a worrying rule's full conditions before claiming the account violates policy. An unread/truncated condition is a missing-evidence finding, not a verified defect. Never repair silently while generating the handover.

## Not covered (not_covered)

List features that are absent, unavailable to inspect, unsafe to demonstrate, or outside scope, and why. Preserve the distinction. Close with per-path readiness, tests actually performed elsewhere, remaining blockers and the next authorized step.

## Evidence appendix

Source ID, tool or client/Vault reference, object IDs, observed time, revision/ETag when available, collection coverage, pagination completion, redaction/truncation and test limitations. Requirements, saved state and executed outcomes must remain distinguishable. Never include credentials, signed private URLs, unrelated customer data or unnecessary lead PII.
