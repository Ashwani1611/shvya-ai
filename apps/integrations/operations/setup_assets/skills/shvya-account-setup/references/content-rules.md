# Content and publication rules

## Standard authoring rules

Always create content as plain text. Customer message bodies, About text and FAQ answers use ordinary paragraphs and line breaks, without Markdown/WhatsApp emphasis, HTML, rich formatting or code fences. Reply options on separate lines and bare URLs are fine. Preserve only the AI Playbook's required headings/message tags and machine-readable tool payload structure; those are parser contracts, never customer formatting.

Always include `{{lead_first_name}}` in every authored customer message body: each Cadence message, Touchpoint, and Playbook welcome, question and acknowledgment block. Use it naturally once per body; it does not require a repeated greeting or a name in every paragraph. Never replace it with `{{lead_name}}`, a literal recipient name or a guessed name. Standalone company facts, FAQ knowledge, internal notes, titles and subjects do not require a recipient token. For approved API templates, map the intended first-name parameter through the supported provider mapping; do not modify approved text or invent a mapping. If no suitable template or resolving surface exists, leave that item unbound and report the gap.

Always create Cadences with `data.is_active: true`, add their steps, and verify they remain active on readback. Keep unfinished Cadences isolated from enrollment and enabled triggers. This standard does not authorize sending, enrollment or enabling Workflows. Preserve the active state of existing Cadences on unrelated updates; a requested disable/archive remains supported.

## Variables are two distinct systems

`{{SHVYA_COMPANY_NAME}}`, `{{SHVYA_PIPELINE_ID}}` and other uppercase `SHVYA_` values are **build-time kit variables**. Resolve them from verified intake/discovery before saving to Shvya. They are not environment credentials, CRM fields or automatically supported runtime merge fields. None may remain in a live Playbook, FAQ, message or tool payload.

Native Shvya substitutions use **double braces**. The inspected Cadence text renderer supports `{{lead_name}}`, `{{lead_first_name}}`, `{{phone}}`, `{{email}}`, `{{lead_source}}`, `{{org_name}}`, `{{user_name}}`, `{{pipeline_name}}`, `{{stage_name}}` and exact keys present in the lead's attributes. These must be checked against the current channel renderer/available placeholder catalogue. Template components have their own provider-variable mapping and examples; do not assume that arbitrary named tokens work there.

A known field with no value becomes an empty string in the inspected Cadence renderer. An unknown token can remain literal. A custom attribute key not present on a lead may therefore leak unchanged. Keep `{{lead_first_name}}` in the authored body and preview both populated and missing-name cases on the actual delivery surface. If missing data produces broken copy, flag the data/binding issue before enrollment; use only an existing verified runtime fallback, never an invented name or conditional syntax. Do not port legacy single-brace fields, `{booked_slot}`, `{lead_phone}` or arbitrary `{{name}}` without a verified mapping.

## Publication gate

For all customer-visible messages, About, Playbook copy, FAQs and Touchpoints:

- No kit placeholders, unknown runtime tokens, `TBD`, `[Insert...]`, internal notes, drafting labels, example domains, fabricated URLs or copied demo values.
- Each customer message body is plain text and contains `{{lead_first_name}}` or its verified approved-template first-name mapping; test the rendered preview as well as the authored body.
- Every price, date, quantity, result, promise, phone, payment detail and offer has provenance and correct scope. Normalize units/currency without changing meaning.
- A testimonial must be real, permitted for use and properly attributed. If unavailable, use a grounded process explanation. Urgency requires a real deadline/capacity mechanism and expiry handling.
- Promises to send a file require an actual delivery route. An indexed knowledge document alone does not establish sendable-media support. Promises of bookings, payments, calls or escalation require a real connected mechanism; otherwise say the team will handle the next step.
- Keep one thought per paragraph, short lines, a blank line between paragraphs, and answer options on separate lines. Default to concise natural copy; exact customer wording wins when requested.
- Language and script follow the configured supported languages and client preference. Mirror a lead's language/script where supported, carrying pending context across switches. Preserve names and technical terms naturally; do not impose crude script bans on proper nouns.
- Default emoji style is none unless selected by the client. If used, keep it restrained and meaningful. Internal entity keys/names stay plain. Plain punctuation is preferred; punctuation style alone must not override client copy requirements.
- No claim of being a human employee. A named persona may identify as the company's AI assistant. No AI diagnosis, guaranteed returns/admissions/outcomes, or unsupported expert conclusions.
- If a cleanup empties required content, block that item for revision. Never publish the original failing content as a fallback.

## Entity quality

**Attributes:** map every required question to meaningful fields when persistence is intended. Use `option` for a true enumerated dimension and `numeric` for quantity comparisons; do not convert numeric MOQ logic into substring keywords. Give a one-sentence extraction description. Record the name, returned key, type, allowed options, requirement ID and reason collected. Existing identity fields should be reused where appropriate.

**Stages:** clear definition of the observed business event, human/AI owner and AI flag. Protected stages are determined by Shvya, not legacy assumptions. Qualified is distinct from Appointment Confirmed, Payment Verified and Won. Do not rename protected stages or invent a color field unsupported by MCP.

**Playbook:** use the canonical eight headings. All narrative routing, questions, criteria and mappings agree. One new question per turn, answer then resume, capture explicit information on first mention, clarify ambiguity once, avoid repeated interrogation, and hand off when a required field is refused or unresolvable. Refusal can terminate questioning without satisfying a hard gate.

**About:** concise stable company information and key policies. Separate pricing facts from permission to disclose them; a `defer` preference does not make stored prices cease to exist. Keep the disclosure rule consistent in all surfaces.

**FAQs:** 2–4 useful sentences with source locator. No invented policy to reach a target count. Use a clear handoff when the approved source lacks an answer. Organize categories in planning if useful, but only send fields actually supported by `upsert_faq`.

**Cadences:** one new useful fact/asset per message, a small first-reply ask, concrete CTA, no presupposed call/reply/purchase, and no repeated material on paths one lead can traverse. Often 30–120 words is enough; shorten when evidence runs out. Promotional and recovery copy should provide an opt-out aligned to the real suppression mechanism. Final recovery messages can offer Interested/Later/Stop without promising future contact that is not scheduled.

**Touchpoints:** typically 12–20 situational saved replies across Initial Response, Info & Links, Follow-Up, Objections, Payment & Next Step, and Closing & Reactivation. Retain only groups with grounded content. A payment detail or booking confirmation must reflect verified state when used.

## Preferences to record

Question wording: natural/verbatim. Length: standard/concise/detailed. Flow: direct/consultative. Product-specific follow-ups: off/on only when needed. Answer posture: answer relevant questions then resume, within disclosure rules. Script: Latin/native/mirror. Tone: professional/premium/warm. Pricing: defer/ranges/full, always sourced. Also record emoji preference, actual allowed languages, handoff policy and consent/opt-out rules.

The client can change authoring defaults; backend permissions, evidence requirements and messaging constraints remain authoritative.
