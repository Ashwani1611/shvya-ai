# Ria Playbook review notes

`shvya-ria-ai-playbook.md` preserves the user-supplied reference, including customer copy and all behavioral edge cases. It is ready for review, not evidence of live configuration. `ai-playbook.template.md` generalizes that structure for other organizations; rendering with `shvya-example.values.json` produces the Shvya example with additional explicit evidence boundaries. Neither file has been applied to a live organization.

## What is grounded

The reference names Ria as Shvya AI's AI assistant and says Shvya AI helps businesses manage leads, automate follow-ups, and respond to enquiries more efficiently. The reference supplies the escalation contacts Ashwini — 8360156287 and Gaurav — 9470225755, four questions, thirteen mappings, three stage rules, and two reminder rules. These are user-provided configuration instructions for review; no website, price, integration list, service promise, availability, testimonial, or performance statistic has been invented.

The following details are preserved:

- Qualification runs only in New Lead / New Leads; all four clear mapped values are required. Optional fields do not block. A refusal, ambiguity, or high AI score does not qualify.
- The most recently asked question owns A/B/C/D answers. Multi-answer messages and explicit corrections are captured. One question, one set of options, no repeated welcome, and one completion acknowledgment.
- Exact boundary mapping: daily 10 → `0-10`; daily 11–30, including exactly 30 → stored `10-30`; greater than 30 → `30+`. Monthly amounts are not daily. The stored `10-30` label deliberately represents the 11–30 question band.
- Currently active paid ads differ from past, planned, or unclear "sometimes" advertising. Multiple tools map to Multiple places; multiple problems are preserved or the main one clarified if the field is single-select.
- CRM platform names, company website, budget units, industry, WhatsApp use, and SOURCE each need explicit evidence. A WhatsApp conversation alone does not establish business WhatsApp use. Shvya's website is not the customer's website.
- An explicit call/demo/human request takes priority over qualification. Ashwini is first; Gaurav follows only after the first contact was sent and further escalation is requested. Both contacts must already have been sent before continued unresolved assistance permits Human Intervention Needed. Queued drafts and inbound contact quotations do not count.
- Human Intervention Needed outranks a repeated Call Requested transition. Missing/inactive/ambiguous destinations leave the stage unchanged. No pipeline changes or backward transitions are authorized.
- Stop, unsubscribe, removal, and not interested stop qualification and automated follow-up. No new reminders. No invented file or repeated unsolicited delivery.
- Callback and later follow-up reminders require an explicit future date/time, honor an explicit customer time zone over Asia/Kolkata, resolve tomorrow in that zone, clarify missing/past/conflicting timing, deduplicate, and confirm recording only after success. An internal reminder is not a confirmed appointment.

## Before a future live application

Use read-only MCP discovery in the explicitly authorized organization to bind the real pipeline, account, stage IDs, attribute IDs, types, and options. The example values intentionally leave identifiers null. The Playbook contains display names because Shvya resolves them against organization-owned records; never invent IDs to fill gaps.

Required definitions are BIGGEST PROBLEM, LEAD MANAGEMENT TOOL, LEADS/D, and RUNNING ADS. Check all optional definitions referenced by mappings. Existing single-option BIGGEST PROBLEM needs a main-problem clarification; a text field may preserve multiple. Do not silently change an established attribute type. Verify exact option translation and the four supported stage labels before applying. Missing definitions are setup work for the authorized operator, not permission for Ria to create fields or options.

The requested live organization, actual contact availability, native CRM definitions, message provider, business hours, source health, and account capabilities have not been discovered by this package. Explicit platform controls must enforce opt-out suppression and human takeover alongside the prompt. A prompt alone does not create Workflows, Cadences, files, reminders, or permissions.

Prefer one `update_ai_configuration` change carrying the complete authored `ai_playbook`. `upsert_qualification_configuration` reconstructs qualification-owned sections; do not run it after saving this Playbook unless replacing those sections is deliberate and the complete resulting Playbook is reviewed. Read back the full Playbook and compiled qualification configuration, then validate and exercise Sandbox cases before activation. Do not use a truncated/redacted readback as replacement content.
