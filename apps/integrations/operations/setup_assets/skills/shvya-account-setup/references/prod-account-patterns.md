# Reference contents

- Transferable production lessons
- Complete source authoring method, adapted for SHVYA
- Prod Account Patterns → Agent Hub Builder Improvements (v2)
- Decisions log (Abhyudaya, 2026-08-20)
- 1. Stages
- The default stage list (embed in the builder prompt as the recommended starting point)
- Industry stage vocabularies (add to industry playbooks)
- 2. Custom attributes
- 3. Sequences
- Launch set (5 per account confirmed as the right target)
- Message-quality rubric (the main v2 deliverable — encode in the sequence-writer agent prompt)
- 4. Rules / smart triggers
- The default rule set to generate (7 rules)
- 5. org_info / qualification requirements
- Scale facts
- The v2 template (Master Prompt, refined by what heavy-AI accounts actually converge on)
- Edge-case & device library (harvest for the generation prompt, applied per vertical)
- Vertical qualification patterns (question-count and gate design)
- 6. Quick replies — spec for the new generation step
- 7. Prioritized builder changes (updated for decisions + v2 data)
- 8. Prod hygiene issues found in passing (worth an ops sweep, separate from builder work)
- Appendix — sources

# Transferable production lessons

The source bundle reported audits of another CRM's clients. Those counts, customer names, conversion assumptions and performance claims are not Shvya evidence. Preserve the design lessons as hypotheses and verify them in Shvya's runtime and the client's process.

- A complete, coherent Playbook matters more than a large About paragraph or a fixed character count. The useful structure is Rules, welcome, parseable questions, acknowledgment, criteria, stage mapping, attribute mapping and reminder logic.
- Described enumerated fields improve consistency when the business truly has fixed choices. Quantities and thresholds need numeric values. Do not create state fields the runtime cannot maintain reliably.
- Strong flows retain answers, support explicit branch changes and do not repeatedly ask refused questions. A refusal remains distinct from satisfying a required evidence gate.
- Human-owned negotiation, complaint, payment verification and clinical/expert decisions need clear handoff behavior. Qualified is not payment completed or appointment confirmed.
- No-response recovery can be useful, but only with correct entry/exit conditions, a finite end, consent-aware re-entry and no overlap with bump-ups or other Workflows.
- A repeated generic check-in earns no additional attention. Each follow-up should offer a new, real fact, useful asset or clear action. Do not pad to a sequence/message quota.
- Named proof, objective specifics and useful logistics are stronger than invented urgency, anonymous testimonials and vague reassurance. If proof is missing, shorten or replace that message with sourced process detail.
- Seed content is scaffolding. It often leaves wrong-industry services, placeholders and unsupported promises. Reconcile it before handover.
- Runtime language, merge fields, ownership, schedule anchors and source filters are implementation details worth verifying. Legacy behavior cannot establish Shvya behavior.
- Configuration success, model quality, retrieval quality, provider delivery and business outcomes are separate claims with different tests. Report which were actually checked.

Useful optional devices, selected per client: stale-quote revalidation, explicit document gates, no repeated confirmation for clear answers, controlled negotiation handoff, multi-product routing, existing-customer routing, supplier/job-seeker classification, contextual acknowledgments, and bounded retries. Do not import another client's hidden discounts, exact thresholds, team names or attribution metrics.

Maintain an evaluation set of realistic flows and update it when observed failures justify new cases. The backend template renderer and canonical validators validate artifacts and selected compiler behavior; a claim of Shvya production improvement requires measured outcomes after a separately authorized rollout.




# Complete source authoring method, adapted for SHVYA

> SHVYA ADAPTATION: Read runtime-contract.md before using this full reference. Preserve this method's detail, but compile its output into native SHVYA schemas. Kraya field names, API routes, database queries, token scripts and past performance claims are historical context, not live capabilities. Sample businesses, prices and policies remain examples. Human handoff/opt-out takes precedence over continued qualification; use verified double-brace CRM tokens and provider bindings. This reference does not authorize sending, enrollment or activation.

# Prod Account Patterns → Agent Hub Builder Improvements (v2)

**Date:** 2026-08-20 (v2 — full healthy-client cohort)
**Cohort:** all 142 healthy paid orgs from the ops healthy-clients sheet (not discontinued / disabled / AWOL; Kraya-internal orgs excluded), every org analyzed via cross-org SQL aggregates; deep qualitative reads on the top ~30 by `ai_messages_14d`. v1 (57 orgs by lead volume) findings retained where confirmed; every v1 number revised here supersedes it.
**Compared against:** the ops build flow — 5 chat agents (prompts in DB `agents` table) + 8-step builder (`agent-hub/src/lib/ops/kraya-builder/`).

## Decisions log (Abhyudaya, 2026-08-20)

- Emoji: keep the current no-emoji stance. `{{lead_first_name}}`: keep mandated.
- Master Prompt template: adopted for org_info generation (see §5 for the v2-refined version).
- `bot_languages`: set by default at generation (the empty legacy orgs predate the feature).
- Quick replies: ADD a generation step (spec in §6). Templates: skip. Pipelines: skip.
- Stages: no count cap — as many as the client needs per brainstorming call; the prompt carries the recommended default list (§1) as the starting point.
- Strategy flag: already exists in Agent Hub client preferences.
- Ignore: hidden "Existing Leads" stage, stage colors.

---

## 1. Stages

### The default stage list (embed in the builder prompt as the recommended starting point)

Frequency across 142 orgs; custom stages start at order 6 (after the 7 auto-created chat-view stages).

| # | Stage | Orgs | ai_switch ON | Note |
|---|---|---|---|---|
| 1 | New Lead | 99% | 84% | universal |
| 2 | Qualified | 98% | 74% | universal |
| 3 | In Conversation | 34% | 53% | |
| 4 | Nurturing | 28% | 85% | highest-AI stage in prod |
| 5 | Good Lead | 44% | 64% | |
| 6 | Hot Lead | 13–20% | **39%** | humans take over here |
| 7 | Lead Won | 54% | 53% | AI-on split — decide per client (payment/onboarding follow-through) |
| 8 | No Response | 71% | **60%** | a WORKING stage, not terminal — AI keeps chasing |
| 9 | Lead Lost | 27% | 12% | AI off |
| 10 | Deleted | 63% | — | parked last (order ~101) |

Only **New Lead + Qualified are true invariants**; the rest are strong defaults (v1 overstated penetration — its subset over-represented template setups). Temperature stages (Hot/Warm/Cold, ~14–21% each) are a bigger real-world pattern than v1 found — dominant in services and manufacturing.

**AI-switch guidance for the builder:** ON for New Lead → Good Lead and No Response; OFF for Hot Lead (call-stage), Lead Lost, Deleted; per-client for Lead Won.

### Industry stage vocabularies (add to industry playbooks)

- **Education/coaching:** canon + Counselling Done, Follow Up. (The canonical list's home turf.)
- **Healthcare/clinic:** Appointment, Visited, No Show, Do Not Pick, Not Eligible, Cold Leads.
- **Ayurveda/wellness (tele-consult + fulfilment):** Call Needed, Call Attempted, Counselling Finished, Dispatched, Delivered, Reactivated, Not Suitable.
- **Real estate/interiors:** Visit Scheduled, Negotiation, Quotation Sent, Warm Lead.
- **Manufacturing/B2B trading:** richest vocabulary — and there is a ready-made exporter/trader playbook cluster live in 5–8 orgs (all AI-off ops stages): *Dealership Approach, Pricing Issue, Catalog Sent – Requirement Pending, PI Sent – Waiting Acceptance, Sample Required, Sample Delivered – Waiting Feedback, Payment Terms Issue, Repeat Order Follow Up, Deleted - Non Qualified*. Reuse verbatim for B2B clients.
- **Travel:** Booking Confirmed, Itinerary Sent, Booked, Quotation Sent, Future Lead, Junk.
- **Retail/D2C:** Order Confirmed, Delivered, Interested, DNP.
- **Gym/fitness:** sales-cycle style — Connected Lead, Meeting, Proposal/Price Quote, Payment, Trial Completed, Closed Won/Lost.
- **Services (salon/studio/finance/astrology):** temperature-heavy — Hot/Warm/Cold Lead, DNP, Human Intervention, Junk.

**Human Intervention / handoff stage:** recurring in sophisticated configs (astrology, restaurant, travel) — a stage that parks the lead and switches AI off via rule (§4 rule 7). Recommend generating it whenever the qualification config has escalation paths.

---

## 2. Custom attributes

- Median **7 human-configured attributes** per org (p75 ≈ 12); 11 orgs have zero. Types: 73% text, 18% dropdown, 5% number. Only **43% carry descriptions** — described dropdowns are the deliberate-configuration marker.
- Common real keys: city (52), name (37), age (34), source, budget range, decision timeline, preferred slot, visit status, treatment type, property type, budget tier, preparation level, "what are you looking for". (`company`, `created`, `course` frequencies are import/template artifacts — not best practice signals.)

**The recipe** (builder should enforce, not just suggest):
1. Identity basics as text/number/date: Name, Age, City, Last Visit Date (only those the flow asks for).
2. **Every qualification dimension as a described dropdown**: Service/Program Interest, Budget Tier, Decision Timeline/Urgency, Customer Type, Lead Temperature, Call/Visit Status. Description = extraction instruction for the AI.
3. **Workflow-state dropdowns the summary job maintains** (the pattern in the best configs, wired to org_info state machines — §5; the AI reads these, it cannot set them): e.g. `payment_status [Pending, Completed, Issue reported]`, `palm_photo_status [Received, Pending, Not required]`, `Counselling_Status [Not Started, Scheduled, Attempted, Completed]`, `needs_human [true,false]`.
4. 1–2 free-text capture fields: Qualification Notes / Main Problem; optionally an AI-generated `Summary` field with a length spec ("20–24 words max").

Gold-standard examples to embed in the prompt (verbatim setups pulled in the analysis): Dr. Gyanendra Sharma Jyotish (16 attrs, workflow-state machine), Ingenious Research (22, full BANT + ops flags), Global India AI (18, all qualification dims as dropdowns), SurKal Ayurved (21, vernacular dropdown values in Marathi), FX Retina (17, agency-vertical dims).

---

## 3. Sequences

### Launch set (5 per account confirmed as the right target)

Named `<Program/Segment> - <Purpose>`; this naming materially improves the rules step's name-match binding.

1. **Welcome / New Lead** — 3 msgs (+5 min, +1d, +2d)
2. **Auto-Followups (no-reply nurture)** — 5–6 msgs at Day 0/1/2/4/6
3. **Ghost Recovery** — 3 msgs; opens low-pressure ("is this still relevant?"), ends with a graceful door-open exit
4. **Post-Call Follow-Up** — 5 msgs Day 0/1/2/4/6 (recap → nudge → questions → value → close)
5. **Re-engagement (fired from the No Response stage)** — completes the highest-volume automation chain in prod (§4)

Post-sale is a distinct genre worth offering when the client closes in-chat: confirmation → save-this-info → review ask → cross-sell → referral.

### Message-quality rubric (the main v2 deliverable — encode in the sequence-writer agent prompt)

From full-text reads of 30 orgs (~500 messages), the rules that separate A-tier from C-tier:

1. **Every message earns its send with exactly ONE new concrete asset** — an offer mechanic, a named testimonial, an FAQ answer, a domain insight, a price/logistics fact, or a next-step recap. If two adjacent messages survive a swap without loss, delete one. Bare "just checking in" nudges are banned.
2. **Specifics or silence.** Prices with currency, counts ("75,000+ patients", "28 branches"), times, salary bands, MOQ, margins. Ban unfalsifiable filler ("many clients see improvements within weeks"). **Never invent a number.**
3. **One CTA per message, executable in one tap**: `Reply *KEYWORD*` with the payoff named ("Reply SPECS → grade sheet"), a lettered A/B/C/D menu (best for B2C), or a phone number. Keyword rotation without content rotation is C-tier; a message with no CTA is broken.
4. **Assign each mid-sequence message one named objection** and answer it head-on with mechanics, not reassurance: price → installments/levers ("adjust weight, simplify one element, dummy tier"); trust → process detail (courier names, tracking, refund policy, "why pay first" answered honestly); "already have a vendor" → backup-supplier framing; "will it work" → sourced testimonial. Voice the objection before the lead does.
5. **Social proof must carry a proper noun** — person + city + platform ("Amit P., Delhi NCR, Practo"), a named firm, or a checkable number. Anonymous quote-marks testimonials are worse than none. Registration-number dumps are not proof (one line beats fifteen).
6. **Urgency must cite a verifiable mechanism**: weekend slots, freight booking windows, price-validity ("pricing holds 15 days from the proposal date"), seasonal stock, clinical progression. Fabricated countdowns ("offer closes in 2 days" on an evergreen drip, "students enroll every 3–5 min") are trust liabilities.
7. **Message 1 demands the smallest possible reply** — a Yes/No diagnostic ("Does the pain travel into the leg?") or a segmenting question, not the pitch.
8. **Answer the unasked logistical question mid-sequence** — day-in-the-life schedule, how-much-cake-per-guest guide, freight-timing education: utility messages that are valuable even if the lead never buys.
9. **The final message says it's the last**, restates the offer/price menu in scannable bullets, and gives a re-entry path ("Reply BATCH when ready") — plus the preference-capture close: "reply Interested / Later / Stop."
10. **Never presuppose a conversation that didn't happen.** Timer-sent drips may reference the lead's *enquiry*, never their (nonexistent) *replies* ("Thank you, that helps me understand you better" on a no-reply lead reads as malfunction).
11. **≤120 words**, hook line → 2–4 short lines or 3–5 bullets → CTA line; bold only numbers, keywords, brand names. Post-quotation and revival check-ins go short and plain (~220 chars).
12. **Language = the lead's spoken register**, consistently: Hinglish for North-India retail/trade/devotional, regional language where the client operates in one (Marathi/etc.), clean English for professionals/B2B — matching gender forms and "aap" register.
13. **`{{lead_first_name}}` in the opener and final message minimum** (per decision: mandated); segment-level content personalization (per-condition/per-motivation sequences) is the stronger form on top.
14. **No duplicate content across sequences a lead can traverse** — three differently-named sequences sharing 80% of copy send exact repeats when rules move leads between them.
15. **Publish QA gate (hard-block):** no `[Insert]`/unfilled brackets, no "(needs a real testimonial from the client, ask ops to fill)", no literal "CTA:" labels or wrapping quotes from the draft doc, no merge tags that can render empty, no zero-message shells, no delays like "522222 days".

Vertical copy anchors: B2B = MOQ/margin/spec/dispatch SLA + trial-order de-risking; clinics = diagnostic triage opener + "send your report/MRI" + no-surgery reassurance; education = placements with named employers + batch date/cap + mentor résumé + FAQ myth-busting for the parent; travel = scene-setting must still land on package/price/date CTA; real estate = location math (₹/sq yd, minutes-to-X, RERA) funneling to VISIT; fitness = identity framing + free-trial + reel proof.

---

## 4. Rules / smart triggers

95% of the healthy cohort has rules (median 6), but **10% of orgs have zero ACTIVE automation** — rules are part of default provisioning, and the builder closing that gap is pure win.

### The default rule set to generate (7 rules)

1. **`["stop"]` keyword → stop_assigned_sequence** (all stages) — 81 orgs, the most universal rule in prod.
2. **Lead Won / Lead Lost stage entered → stop_assigned_sequence** — 50 orgs (v1's biggest miss); one rule per terminal stage, stops sequences messaging closed leads.
3. **Each stage → initiate its matching sequence** — one rule per (stage, sequence) pair the builder created; 78 orgs, ~4.5 rules/org. Generate mechanically from the created-sequence ID map, not by model invention.
4. **no_response 24h (from New Lead) → move to "No Response" stage** — modal prod config (24h; 48–72h second).
5. **"No Response" stage entered → initiate the Re-engagement sequence** — rules 4+5 form the chain that carries 63% of ALL rule executions in prod (230k fires/30d).
6. **Re-engagement sequence_completed → move to Cold/Uninterested** — 20 orgs; terminates the loop cleanly.
7. **Human-handoff stage entered → toggle_ai OFF + stop_assigned_sequence** — the marker of every sophisticated setup (Barbeque Spice, Taira).

Optional when lead-form integrations exist: new_lead_created → welcome sequence (22 orgs). Power add-on for reactivation-heavy verticals: Cold + any-reply wildcard `["*"]` → Reactivated stage + AI ON (Taira's loop).

**Do NOT ship:** canned intent-keyword → Qualified rules (only 6 orgs actually route keywords to Qualified; destinations are org-specific and **38% of keyword rules never fire** — keyword lists rot), send_email / set_lead_attribute rules (<5 orgs), call_logged triggers (2 orgs), ghost-sequence chaining by default (18 orgs enabled today; 23 built it then disabled it — the no_response loop is the real power pattern).

Distribution corrections vs v1: keyword_detected ties lead_moved_to_stage in org reach (77–78%); new_lead_created outranks sequence_completed; 26% of enabled rules fired zero times in 30d.

---

## 5. org_info / qualification requirements

### Scale facts

- `qualification_requirements` under ~2k chars ≈ an org that doesn't use AI (floor effect). All top performers sit in the **8k–20k band** — target it; >50k is catalogue-stuffing, not structure.
- Compact B-grade configs (Rashi Fashion 5.9k) still drive top-10 AI usage when the funnel is simple — completeness should scale with funnel complexity, not be maximal by default.
- `about` median ~2.2k and is itself templated in newer builds (Company Name / Industry / One-Line Summary / Primary Customer Goal / Core Services / Typical Situations / Hesitations / Tone / Safety & Guardrails). Never inverted (all content in `about`, stub QR — observed anti-pattern).
- `bot_languages` set in 78% of healthy orgs and near-universally among activated ones → generation-time default confirmed as pure hygiene.
- `sendable_files` null for ~60%; heavy use is vertical-driven (travel/retail file libraries, 16–50 files). Generate sendables (with send-trigger descriptions) when the client supplies brochures/price lists.

### The v2 template (Master Prompt, refined by what heavy-AI accounts actually converge on)

**Universal spine (load-bearing — every heavy-AI config has these):**
1. Numbered **Rules** section (10–25)
2. **Welcome message** (verbatim, in tags)
3. Sequential **question blocks** (verbatim `<question_content>`, lettered/numbered options, per-answer validation, conditional branches)
4. **Acknowledgement** message
5. Explicit **qualification criteria** ("qualified when X, Y, Z captured" + do-NOT-qualify list)
6. **Stage-shift logic** (condition → exact CRM stage name; 7/9 deep-dived orgs)
7. **Never-re-ask machinery** (8/9): attribute pre-fill mapping, capture-on-first-mention, answer-then-resume, and Kiwoo's **UNIVERSAL SKIP RULE** — refusals and partials are terminal, never re-ask/rephrase/circle back
8. **Keyword→option mapping** incl. vernacular synonyms ("dukaan"→Retailer) and letter/number equivalence ("a" = "1")

**Promote to first-class section (v2's biggest structural finding): the state machine.** The strongest configs define typed attributes with enumerated values:
- anti-premature-qualification: the transcript itself must carry the answers before Qualified is set, because the attributes behind that decision are written afterwards and cannot be checked first
- artifact-gated transitions (payment link withheld until required photo received; payment-proof = screenshot AND typed "Done" double-confirmation)
- decoupled multi-stage outcomes (`Qualified` ≠ `Payment Done`)
- decline-path routing (payment "No" → `Call Needed` stage, not Lost)
- stage-gated AI whitelist ("AI allowed ONLY in pipeline P1 stages New Lead/Reactivated; else silent")
- auto-cold after 72h + re-entry modes (data <60 days: one recall question → straight to Qualified; else re-ask Q1 only) + low-intent block ("ok/thanks/👍 → stay Cold, no reply")

**Quality multipliers** (include; not activation-critical): persona/identity block (lives naturally in `about`), edge-case handling as numbered rules (a formal Situation|Action table was an exemplar idiosyncrasy — numbered rules do the same job), ✅/❌ paired examples on fragile rules, catalogue tables where the vertical needs them, retry guards (≤2 asks, 1 nudge, escalation path), lead scoring + temperature bands, Company Facts approved-claims list.

### Edge-case & device library (harvest for the generation prompt, applied per vertical)

Core taxonomy (v1, confirmed): early price ask (deflect once, answer-only message separation) · human/callback request (warm handoff + keep qualifying + escalation matrix on repeat) · timeline/guarantee demands (scripted non-committal) · abusive → disengage+flag · out-of-catalogue → "let me check", never fabricate · multiple products → "which first?" · ineligible ≠ disinterested (distinct warm closes) · hard vs soft disinterest → different stages · out-of-service location · language AND script mirroring (Devanagari↔Roman; internal notes English) · bot-reply detection · existing-client detection · booked-call reframe ("helping the consultant prepare") · never repeat a message · menu session-lock (number-replies pinned to current menu).

New devices from the 17-org deep-dive (pick per vertical):
- **Flow control:** anti-reconfirmation rule (soft-confirms are valid; never ask "confirm?") · refusal sentinel ("Not disclosed by lead" fills the slot, question counts as satisfied) · response-ordering micro-protocol (1-line ack → answer their question → next unanswered question) · fuzzy/typo mapping with examples ("Coorge"→Coorg; ambiguous → ask, never guess) · document-gated qualification (Qualified on receiving RC/policy copy, then never ask for it again)
- **CRM automation in-prompt:** field normalization tables (synonym → canonical value + fallback) · name auto-split · AI-written summary field with length cap · timestamp discipline (system time only, at named transitions) · inbound-image handling (payment screenshot inference; OCR invoices/visiting cards; audio-message deflection "please type it")
- **Non-lead routing:** agent/competitor detection → Agent stage · reverse-lead classification (suppliers/transporters → Procurement/Logistics scripts, no purchase commitments) · influencer/vendor capture-and-escalate · B2C-on-a-B2B-funnel → website + B2C stage
- **Sales mechanics:** closed FAQ whitelist ("closed list — anything else gets the fallback, no improvised answers") · hidden negotiation levers ("do NOT mention the extra 5% discount"; margins stay with sales) · below-MOQ re-ask guard vs low-quantity auto-disqualify (pick per client) · stale-quote guard (rates >3 days old must be revalidated) · negotiation band with a floor · cross-sell timing ladder (never on first refusal) · derive-don't-ask (compute buffet type from date+slot) · business-hours window with always-send welcome exception · in-prompt bump-up ladder (12h nudge → 10h close-out → Lost) · review-request gating (qualified AND satisfied only)
- **Tone/robustness:** banned-filler blacklist ("Great!", "Perfect!"…) · humanization quotas (ack ~60% of turns, 1 filler word/chat, emoji budget) · grammatical persona constraints (gendered verb forms for named personas) · vocabulary rules ("Students, not leads") · occasion-aware question relevance ("Baby Shower → do NOT ask age/gender") · tiered information disclosure (a "do not volunteer" list — price/address/medicine released only on matching intent) · link-hygiene rule with valid/invalid examples + no-link-fabrication fallback

### Vertical qualification patterns (question-count and gate design)

| Vertical | Qs | Qualified when |
|---|---|---|
| B2B trading/wholesale | 4–7, branched by buyer type | Volume/fit gate — quantity vs MOQ encoded numerically; qualified after core 4 |
| Education/admissions | 4–5 | Profile + intent (background, program, timeline) |
| Services/astrology/clinic | 2–4 | Deliverable inputs collected (birth details, symptoms, reports); payment as separate stage |
| Travel/D2C | 1–3 | Routing — one answer (destination/category) triggers the right catalogue/team; optional Qs must NOT delay qualification |

---

## 6. Quick replies — spec for the new generation step

Today 71% of orgs run the 4 seeded defaults, which still contain unfilled `[briefly mention key services]` placeholders — filling those with real business content is already a lift. Real customizers converge on 12–25 replies.

**Generate 15–20 replies in 6 function-based groups** (add per-product opener groups only for multi-product clients):

1. **Initial Response (3–4):** business-specific greeting with real USP/social proof; "Call Not Picked"; "Request a quick 5-min call"; after-call recap.
2. **Info & Links (3–4):** Location/address; Pricing/packages with real prices; catalog/brochure link (+ attachment slot); top-FAQ answer — title the reply as the customer's question.
3. **Follow-Up (3):** escalating Msg 1/2/3 — value recap → slot-hold urgency → final "releasing your slot" close; reference the prior conversation, never generic "checking in."
4. **Objections (3–4):** price (investment reframe), "need time" ("don't let thinking become delaying"), "discuss with family" (offer a shareable summary), + one objection-probe question ("what's stopping you — budget, timing, or something else?").
5. **Payment & Next Step (2):** payment details/UPI + screenshot ask; booking-confirmed welcome.
6. **Closing & Reactivation (2–3):** polite closure; ghost "reply once" re-open; review/feedback ask.

Style: 150–450 chars; `{lead_name}`/`{user_name}`/`{org_name}` variables; concrete specifics, no placeholders; **generate in the org's customer-facing language** (Marathi/Hindi/Hinglish sets are common and effective); titles are operator-facing situation labels ("Call Not Picked", "Fee is high", "Delayed Parcel") so staff find them in two seconds; one reply = one job. Power-tier pattern for multi-product clients: per-product `<Product> - Objections` (fee/busy/need-time/family/will-it-work) + `<Product> - Short Nudges` groups (The Virtued/E-Physioneeds architecture).

---

## 7. Prioritized builder changes (updated for decisions + v2 data)

**P0 — org_info step**
1. Rebuild around the v2 template (§5): universal spine + state-machine section + never-re-ask/UNIVERSAL SKIP + keyword→option mapping; edge cases as numbered rules seeded from the device library per vertical; retry guards; `bot_languages` always set.
2. `about` follows its own template (Company/Industry/Summary/Goal/Services/Situations/Hesitations/Tone/Guardrails); never invert content into `about`.
3. Target 8k–20k chars scaled to funnel complexity; a simple funnel gets the lean core, not padding.
4. Qualification-gate design per vertical (§5 table): volume gate for B2B, deliverable-inputs for services, routing for travel/D2C — with optional questions explicitly marked non-blocking.

**P0 — sequence writer QA gate**
5. Hard-block list from rubric rule 15 — placeholder text, `[Insert]`, "CTA:" labels, empty-renderable merge tags. This is currently shipping to production leads in 7 orgs (§8).

**P1 — sequence writer quality rubric**
6. Encode §3's 15 rules as the writer's checklist; per-message role assignment (arc slots); vertical copy anchors; 5-sequence launch set incl. Re-engagement-from-No-Response.

**P1 — rules step**
7. Ship the 7-rule default set (§4), generated mechanically from created stage/sequence IDs. Drop canned intent-keyword rules from the default.

**P2 — stages step**
8. Embed the §1 default list + AI-switch guidance + industry vocabularies (incl. the B2B trading playbook and the Human Intervention stage); count remains per-client.

**P2 — attributes step**
9. Enforce described dropdowns for qualification dimensions; add workflow-state attributes wired to the org_info state machine; embed the 5 gold-standard examples.

**P2 — quick replies (new step)**
10. Implement per §6 (new extractor schema + `POST` quick-replies/groups endpoints; also delete the dead quick-reply prose from playbook FAQ chunks so it stops confusing the FAQ extractor).

**P3 — sendables**
11. Map client-supplied brochures/price lists to `sendable_files` with send-trigger descriptions.

---

## 8. Prod hygiene issues found in passing (worth an ops sweep, separate from builder work)

1. **Placeholder leak, live in customer-facing sequences of 7 orgs** — literal "(needs a real testimonial from the client, ask ops to fill)" in BEFACH (2 seqs), Rashi Fashion (3), The Bakistry (3), Mind Over Cancer (1), Harihar Jyotish (2), Kiwoo (3), RK Advisory (1). Same authoring pipeline (likely Agent Hub sequence flow) — sweep + fix, and the P0 QA gate prevents recurrence.
2. **Vertical-blind starter trio** (Discount/Offer + FOMO Reminder + DNP) live near-verbatim in UFC GYM, SMILE-U-DENT, RK Advisory, VFit — including "healthcare provider" boilerplate in a dental clinic.
3. Config bugs: Amruta Mane "Done customer" msg delay = 522,222 days + ops data-collection message mid-nurture; E store "5-Days" sequence actually fires over 50 days; Mind Over Cancer RISE #12 leaks `{user_name}` merge tag; Techno India org_info Free-Text Handler carries different phone numbers than its acknowledgement; Akshar org_info has contradictory MOQ thresholds (200 vs 1000) + missing rule 19.
4. **14/141 healthy orgs have zero active rules** (7 none, 7 all-disabled) — candidates for an ops enablement pass with the §4 default set.
5. VFit sequences ask cold leads to GPay ₹999 to a personal number with fabricated urgency — trust/brand risk worth flagging to the POC.

## Appendix — sources

v2: 142-org healthy sheet cohort; stages+attributes aggregate agent; rules aggregate agent (+ 30d rule_executions); 2 sequence-quality deep-read agents (30 orgs, ~500 full messages); 2 org_info deep-dive agents (17 heavy-AI orgs + 140-org tally); quick-replies agent (142 orgs + power-user deep dives). v1: 57-org lead-volume cohort, 5 domain agents + builder-prompt audit (`steps.ts`, `api-docs.ts`, `industry-playbooks.ts`, `schemas.ts`, `extractor.ts`).
