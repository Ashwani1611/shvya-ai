# Content rules and defaults for a Kraya account

Everything in this file is enforced somewhere in Agent Hub today, either in a prompt, an extractor preamble, or deterministic code. Sources: `kraya-builder/api-docs.ts`, `steps.ts`, `sanitize.ts`, `schemas.ts`, `client-preferences.ts`, `preference-snippets.ts`, `industry-playbooks.ts`, `Kraya-Laravel` models.

## 1. Publish gates (deterministic, applied to everything lead-facing)

Apply to sequence messages, quick replies, org info `about` and `qualification_requirements`, FAQ answers, bump-up messages.

- **Emoji: client's choice, default none.** Ask on intake whether the client wants emoji in lead-facing messages. If they did not ask for them, strip all pictographic emoji, dingbats, keycaps and flags (this is what Agent Hub's builder does unconditionally). If they did, keep them but apply the writer's rules: 1–2 per message (up to 3–4 for a client who asked for a heavy style), placed next to the phrase they echo (📍 by an address, ✅ by a confirmation), never clustered at the end as a sign-off, never as bullet markers in option lists, consistent across sequences, qualification messages, quick replies and bump-ups. Emoji never go into stage names, attribute keys or values, rule names, sequence names, or FAQ titles: those are matched by string in code and prompts. Text arrows `→ ← ⇒` (U+2190–21FF) are always kept because 72% of accounts use them in keyword-mapping rules; emoji-picker arrows (➡ ⬅ ➔ …) become `->`.
- **No em or en dashes.** ` — ` and ` – ` mid-sentence become `, `; a dash at line start becomes a `- ` bullet; anything left becomes a comma. Regular hyphens stay (follow-up, walk-in, 24/7, bullet markers).
- **Placeholder gate.** A message is dropped, never shipped, if it contains: an ops note in parentheses (`(needs a real testimonial…)`, `(ask ops to fill)`, `(no draft…)`, `(placeholder)`), a bracketed placeholder (`[Insert link]`, `[add price]`, `[your name]`, `[TBD]`), a literal `CTA:` label, a `<<template placeholder>>`, any merge field outside the allowed set below, any double-brace merge field, or the token `TBD`.
- **Merge fields use single braces.** Kraya substitutes `{lead_first_name}`, `{lead_name}`, `{user_name}`, `{org_name}`, `{email}`, `{lead_phone}`, `{booked_slot}` and `{<Attribute Key>}` (a custom attribute's exact key, e.g. `{Course Interested In}`) in sequence messages, quick replies, bump-ups, reminder and email messages. It does this with a plain string replace, so a double-brace `{{lead_first_name}}` renders to the lead as `{Rahul}` on every channel (verified against `AutoResponderService::parseContent` and the extension's formatter on 2026-09-10). Double braces are only correct inside WhatsApp *template* components (`{{first_name}}`, Meta's variable syntax). Prefer `{lead_first_name}` for greetings; attribute merge fields are fine when the attribute is filled before the message fires (a missing value renders as empty text, so never let a sentence depend on it).
- **Placeholder URLs.** `example.com`, `yourdomain.com`, `localhost`, reserved TLDs, and bare tokens without a dot (`TBD`, `pending`) are removed from knowledge-base attachments. A fabricated URL indexes nothing and silently leaves the knowledge base empty.
- **Script purity on multilingual accounts.** Every lead-facing message must be written only in the scripts the account's `bot_languages` actually needs (Latin plus, typically, Devanagari and the client's regional script). Reject a message carrying characters from any other writing system, in particular Arabic, Cyrillic, CJK, Bengali and Thai, and reject a stray word from an unrelated language even when it is in Latin script (`nossa`, `vuestro`). Never switch script part way through a conversation, and never mix two scripts inside one message: a lead who writes Hindi in Latin letters is answered in Latin letters, not Devanagari. Technical tokens stay Latin in every language (`GSM`, `kg`, `ft`, `approx`, `pincode`, `GST`). This is a model artifact rather than a prompt bug, so a spec rule mitigates it but does not close it: on any account whose `bot_languages` includes a non-English language, run the demo chat once in each of those languages before handover, scan the replies for out-of-range characters, and escalate to whoever owns the org's prompt-config model if any survive. Observed on a live production account in September 2026, where Gujarati replies returned `تقريباً`, `вашей`, `因素` and `nossa` across separate runs while the Hindi and Hinglish replies on the same configuration stayed clean.
- **Required fields never emptied.** If stripping would blank a required field (a pure-emoji message on a no-emoji account), keep the original and flag it for a human rather than sending an empty string.

## 2. Per-entity specs (what "good" looks like)

### Custom attributes
Derive from four sources: every qualification question with options → a `dropdown` (key = question topic, values = options); workflow-state attributes from the qualification's Attribute & State Mapping reproduced with the exact names and enumerated values; lead identity fields the flow collects (Name text, Age number, City text, Email/Phone when asked); the industry intent field (`Treatment Interested In`, `Course Interested In`, `Property Type`, `Product Category`). Prefer dropdown whenever the value set is enumerable. Every attribute has a one-sentence `description` written for the AI ("The lead's decision timeline, set from Question 3; phrases like next week map to This month"). Keys are Title Case with spaces, charset `[A-Za-z0-9 _./&-]`, max 150; write "Company and GST", never "Company + GST". Aim for 6–12.

### Stages
Kraya seeds the inbox views (All Chats, Unread Chats, Needs Reply, Groups, Pending Reminders, Queue; `is_default: true`, orders 0–5) and the funnel stages New Lead, Qualified, Nurturing, Good Lead, Lead Won, No Response, Deleted (orders 6+; Deleted at 100; all with `ai_switch: true` and a default description). Older orgs may show Lead Lost instead of Good Lead. Read them from `/users/metadata`. They are a starting point: describe, colour, toggle AI, reorder, or delete them to fit the client's funnel. Only the names New Lead and Qualified are fixed (Laravel matches them by name). Custom stages go strictly after Qualified.

Default funnel the healthiest accounts converge on: New Lead (AI on) → Qualified (on) → In Conversation (on) → Nurturing (on) → Good Lead (on) → Hot Lead (**off**, human closes) → Lead Won (per client) ; No Response (on, a working stage) ; Lead Lost (off) ; Deleted (off). Add **Human Intervention** (off) whenever the qualification has a handoff path. Every stage named in the qualification's Stage Shifting table must exist with the exact same name. Industry vocabularies:

| Industry | Post-qualification stages |
|---|---|
| Healthcare | Appointment Booked, Visited, Treatment Started, No Show, Do Not Pick |
| Tele-consult + fulfilment | Call Needed, Counselling Done, Dispatched, Delivered, Reactivated, Not Suitable |
| Real estate | Site Visit Scheduled, Site Visit Done, Negotiation, Quotation Sent |
| Education | Counselling Booked, Demo/Trial Scheduled, Enrolled, Payment Pending |
| Travel | Quotation Sent, Itinerary Sent, Booking Confirmed, Future Lead |
| Retail / D2C | Order Confirmed, Delivered, DNP |
| Manufacturing / B2B trading (AI off on all) | Quotation Sent, Sample Required, Sample Delivered - Waiting Feedback, PI Sent - Waiting Acceptance, Payment Terms Issue, Order Confirmed, Repeat Order Follow Up, Dealership Approach |
| Fitness | Trial Scheduled, Trial Done, Membership Active |

Colours: blue `#2196F3` in-progress, amber `#FFA726` action needed, green `#4CAF50` won, red `#F44336` lost, purple `#9C27B0` parked. Kraya seeds every stage in black, so send a colour on seeded and custom stages alike. Every stage gets a one-sentence description written for the AI ("Leads who confirmed a consultation date; move here when a slot is agreed"): the runtime hands the AI each stage's id, name and description when it decides `change_stage`, so an undescribed stage is one the AI cannot use correctly. Seeded stages default to AI on; turn it off on Lead Won, Deleted, Hot Lead and any stage a human owns.

### Sequences
Five core sequences per account, named `<Segment> - <Purpose>`: DNP / No-Response Recovery (fired from the No Response stage), Interested Nurture, Lead Lost / Dormant Revival, Validation, Call Booked. Optional: Promotional (only with a real offer) and No-Show Recovery (only when calls are the conversion step). Day counts by sales cycle: short 4/5/4/3/2, medium 5/7/5/3/3, long 6/8/6/4/3 for DNP/Nurture/Lost/Validation/Call Booked. Shorten when the asset pool runs dry.

Message rules: 2–4 short paragraphs separated by blank lines; `*bold*` on product, brand, price, key terms; `-` bullets for 3–5 item lists of short noun phrases; one concrete asset per message and no asset repeated across the account; quantify or delete, never invent a number; social proof only with NAME + CITY (or NAME + ROLE); urgency only with a verifiable mechanism; one CTA of the form `Reply *KEYWORD* to <payoff>`; phone CTA line for B2B / high-ticket; `Reply *STOP* to unsubscribe.` as the last paragraph on marketing nurture and re-engagement; first message greets with `Hi {lead_first_name},`, later ones start with substance; never presuppose a reply; the last DNP / Lost / No-Show message declares itself the last and captures Interested / Later / Stop. Write in the lead's language and register (Roman-script Hinglish, Punjabi, Marathi as the client's customers actually text). 60–150 words. First message delay usually 5 minutes; nurture 1–3 days between messages; last-chance 5–7 days.

### Org info
`about` carries every non-negotiable fact: one-line summary, services/programs/products with names, target audience, locations and service area, pricing or pricing model, website, phone/WhatsApp, email, founder/team credentials, policies the AI must respect. Never a placeholder domain. `attachments` = the client website plus real document URLs (each is scraped into the knowledge base). `bot_languages` = comma-separated language names the customers use; default `English`. `qualification_requirements` = the full spec with every section the qualification builder produced, formatting intact: 2–4 paragraph messages, options one per line, `*bold*` brand and category labels, no dashes, emoji only if the client asked for them.

### FAQs
15–30 articles grounded only in the client's documents, website, profile and qualification flow. Categories only where supported: Pricing, Process, Eligibility (only with real rules), Logistics, Trust, Services (one per major service), Objections. 2–4 specific sentences; quote prices, names, timings verbatim; when the source has no answer, write a hand-off ("Please share your details and our team will get back with specifics"), never an invented fact. No generic template questions (age limits, "sessions online or in-person", "recorded classes") unless the client's materials mention them.

### Rules
Core set built from real ids for every account (see `api-reference.md` §11): stop on STOP keywords, stop on Won/Lost, silence 24/48/72h → No Response, No Response → DNP sequence, DNP completed → dormant stage, Human Intervention → AI off + stop sequence, and one stage → sequence rule per pair. Silence timer by sales cycle: short 24h, medium 48h, long 72h. Consolidate: one rule with many `trigger_conditions` rather than N rules. Never create intent-keyword → Qualified rules; never reference an id you did not read from the account.

### Quick replies
15–20 in six groups: Initial Response (3–4: greeting with a real USP, Call Not Picked, quick-call request, after-call recap), Info & Links (3–4: location, pricing with real numbers, brochure line, top FAQ), Follow-Up (3, escalating), Objections (3–4 from the profile's verbatim objections plus one probe), Payment & Next Step (2), Closing & Reactivation (2–3). 150–450 characters; `{lead_name}`, `{user_name}`, `{org_name}` variables (quick replies use single braces, sequences use `{lead_first_name}`); names are operator-facing situation labels ("Call Not Picked", "Fee is high"); customer-facing language; every reply carries a real specific. Replace the 4 seeded defaults (Acknowledgment, Scheduled Call Reminder, Follow up 1, Follow up 2), which still contain `[briefly mention key services]` placeholders.

### Bump-ups
Kraya seeds five fixed steps at signup: "Hey, just bumping this up - let me know 🙂" (+5 min), "Seems like I lost you there - just need 2 mins for the above, whenever you're free" (+60), "Would it be easier to just jump on a quick call and go over this?" (+120), "Just checking in again - you can also reply here or share a good time and I'll call you" (+180), "Haven't heard back from you - if you're free anytime, just drop a message or give me a call and we'll take this ahead" (+480). Orgs run in AI mode by default (`fixed_bump_ups: false`). Rewrite the fixed steps in the client's language and register when the client wants fixed nudges, keep the seeded defaults' brevity. The customer-facing bump-up endpoint is staging-only for now; on production, note the seeded steps (step 1 has an emoji) as a gap for ops.

## 3. Workflow-state machine (qualification requirements)

The best accounts treat the conversation as a state machine over attributes:
- Question attributes: one named attribute per question; a two-part question maps to two attributes.
- Workflow-state attributes with enumerated values: `Qualification Status [Pending, Completed]`, artifact statuses (`Report Status`, `Photo Status`, `Payment Screenshot Status [Received, Pending, Not required]`), `Callback Requested [Yes, No]`. The summary job extracts these after the reply lands; the AI reads them and never sets them.
- Attributes are read-only to the AI and lag one turn. The reply carries a message and a stage change, nothing else, so never write an instruction telling the AI to set, write, verify or order an attribute write. Every stage-shift condition must be decidable from the conversation in the current turn.
- Artifact gates: a dependent step waits on the artifact arriving in the conversation, with one scripted re-ask and a graceful fallback; payment is confirmed only by a human. Gate on what the transcript shows, never on an attribute that may not have landed yet.
- Capture on first mention; never infer one field from another.
- Every instruction has its material: a rule that tells the bot to answer or share something (RERA, floor plans, event dates, payment links, a brochure) is only written when the value is in org info or an FAQ, or the file is a sendable file referenced by UUID. Otherwise write it as a hand-off. See `conflict-audit.md`.
- Never-re-ask machinery: universal skip, refusal sentinel ("Not disclosed by lead"), anti-reconfirmation, one clarification per question, no message repeated.
- Stage Shifting table: one row per real outcome (all required answered → Qualified; firm decline → Lead Lost; asks for a human → Human Intervention; ineligible → Lead Lost after a polite close; client-specific rows). Silence is never a row; Kraya rules handle silence.
- Edge cases as numbered rules: early price ask, timeline/guarantee demands, multiple services, out-of-catalogue, hard vs soft disinterest, ineligible lead, abusive/spam, existing customer, already booked a call, off-topic mid-flow, plus vertical ones (never diagnose; below-MOQ guard; no admission guarantees; no ROI promises; fuzzy destination mapping).

Target size for `qualification_requirements`: 8–20k characters scaled to funnel complexity. Under 2k means the account will not use AI meaningfully; over 50k is catalogue stuffing.

## 4. Qualification gate types (choose the flow shape)

| Gate | Businesses | Shape |
|---|---|---|
| Volume/fit | B2B trading, wholesale, manufacturing | branch by buyer type; decisive question is quantity vs MOQ; 4–7 questions |
| Deliverable inputs | services, clinics, astrology, consulting | the questions are the fulfilment inputs (symptoms, reports, birth details); payment is a separate stage |
| Profile + intent | education, coaching, finance | background → program interest → timeline; 4–5 questions |
| Routing | travel, D2C retail, multi-brand | one mandatory routing question; everything else optional and non-blocking; 1–3 questions |

## 5. Industry playbooks

Nine playbook keys with six chunks each (stages, attributes, sequences, orgInfo, faqs, rules, quickReplies): `healthcare`, `realestate`, `fitness`, `travel`, `b2b`, `agencies`, `education`, `retail`, `manufacturing`. Full text in `industry-playbooks.ts`. Use the chunk for the step you are on as a template and adapt to the client; never copy its scenarios when they do not match the business audit. Detection rule of thumb: retail sells finished products to consumers, manufacturing supplies goods to businesses (samples, quotes, MOQ), b2b is a service or software sold to businesses, agencies are marketing/creative/media services, a study-abroad consultancy that mainly does visas is b2b while admissions/test-prep is education.

The Laravel onboarding seeds a different, coarser set (`education_training`, `healthcare_medical`, `real_estate_construction`, `travel_tourism`, `manufacturing_industrial`, `other`), documented in `industry-templates.md`. Those are what an account already contains when you receive it.

## 6. Client preferences (8 closed questions)

Asked on the brainstorming call; the first option is always the default and emits nothing. Only non-default answers add an instruction to the qualification build (and, for length/tone, to FAQs and sequences):

| Key | Options (default first) | Non-default effect |
|---|---|---|
| questionWording | natural, **verbatim** | reproduce the client's exact wording in `<question_content>` tags |
| messageLength | standard, **concise**, **detailed** | ≤2 paragraphs and ≤6 options, or 3–4 substantive paragraphs |
| flowStyle | direct, **consultative** | give one useful fact about the chosen option before asking for the booking |
| perOptionFollowUps | off, **on** | 2–4 follow-up questions specific to each chosen option |
| answerPosture | collect, **answer** | answer from the knowledge base first; never affirm ungrounded claims; under `defer` pricing, still never state prices |
| languageScript | latin, **native**, **both** | write in native script / mirror the lead's script; plus turn-by-turn language mirroring that carries the pending question across a switch |
| tone | professional, **premium**, **warm** | measured, no urgency or discount language / acknowledge the lead, use their name once known |
| pricingDisclosure | defer, **ranges**, **full** | starting-from bands only / exact prices from the knowledge base only, never estimated |

Emit the block as:

```
<client_preferences>
The client explicitly chose how their bot should behave. These override the corresponding general guidance above. Apply every one of them.

- <instruction per non-default answer>
</client_preferences>
```

Emoji is not one of Agent Hub's eight questions (its builder strips them for everyone). For the cloud agent it is a client choice captured on the brainstorming call: none unless asked, then the placement rules in §1. Escalation numbers and media links are facts from the checklist, not preferences. Which languages the bot speaks is `bot_languages`, not a preference.

## 7. Sizing table (what a complete account looks like)

| Entity | Target |
|---|---|
| Custom attributes | 6–12, all described, dropdowns for every enumerable dimension |
| Custom stages | as many as the client's process has; default funnel + Human Intervention + industry stages |
| Sequences | 5 core (+ up to 2 optional), 4–8 messages each |
| Org info | `about` 1.5–3k chars; `qualification_requirements` 8–20k chars; website + docs in attachments; `bot_languages` set |
| FAQs | 15–30 grounded articles in 4–7 categories |
| Rules | 7 core + one per (stage, sequence) pair; typically 10–15 |
| Quick replies | 15–20 in 6 groups |
| Bump-ups | AI mode, or 3–5 fixed steps in the client's language |
