# Kraya Brainstorming Call Checklist

**Purpose:** Run this checklist with every new client on the brainstorming call. The output of this call feeds directly into the client's Kraya account setup — specifically the 9 per-org configurable items: Qualification Requirements, Org Information, Knowledge Base, FAQs, Smart Triggers, Attributes, Stages, Sequences, and Integrations.

**How to use:** Each section maps to a specific Kraya configuration area (shown in *italics*). Ask every question. Capture the answer in the format indicated. If the client is unsure, park it and return at the end — but do not skip.

**Output of this call:** A populated brainstorming doc that lets ops confidently build (or clone + tweak) the account without needing to go back to the client for basics.

---

## Pre-Call Prep (Ops Member)

- [ ] Read any prior notes from the sales handoff (what was pitched, which features were promised)
- [ ] Check if the client's industry already has an existing account template we can clone (e.g. beauty/wellness, coaching, real estate, edtech, healthcare) — saves 60% of setup time
- [ ] Join the call on Fireflies so the recording auto-generates a KB draft later
- [ ] Have this checklist open and a blank brainstorming doc ready

---

## Section 1 — Business Context
*Feeds: Org Information, Knowledge Base*

- [ ] **What does your business do?** (1-sentence elevator pitch)
- [ ] **What products / services do you sell?** (list each with a one-line description)
- [ ] **Who is your ideal customer?** (ICP: industry, role, company size / buyer profile)
- [ ] **What is your USP?** (what the AI should emphasize in every conversation)
- [ ] **Location / service area** (pan-India, specific cities, global, online-only?)
- [ ] **Languages the AI should respond in** (English only? Hindi? Hinglish? Regional?)
- [ ] **Business hours + time zone** — *(also feeds Auto Followup business-hours setting)*
- [ ] **Any compliance-sensitive topics?** (e.g. medical claims, financial advice, legal disclaimers)
- [ ] **What should the AI NEVER say or promise?** (hard guardrails)
- [ ] **Website URL + any brochures, pricing sheets, case studies, testimonials to ingest into the KB**

---

## Section 2 — Lead Sources & Volume
*Feeds: Integrations, Custom Attributes, WhatsApp Widget*

- [ ] **Where do leads currently come from?** Tick all that apply:
  - [ ] Meta (Facebook / Instagram) Lead Ads
  - [ ] IndiaMART
  - [ ] JustDial
  - [ ] Housing.com / other marketplaces
  - [ ] Website contact form
  - [ ] WhatsApp click-to-chat ads (CTWA)
  - [ ] Existing CRM (Zoho / GoHighLevel / HubSpot / Salesforce / other)
  - [ ] Google Sheets (manual entry or ad export dumps)
  - [ ] Cold outbound / manual import
  - [ ] Referrals / walk-ins
  - [ ] NeoDove / other telephony
  - [ ] Other: ______
- [ ] **Expected daily / monthly volume per source** (helps size credits & plan)
- [ ] **Existing leads to migrate?** (one-time CSV or ongoing live sync?)
- [ ] **What metadata comes with each lead?** (source name, UTM, campaign, ad creative, form fields) — each becomes a custom attribute
- [ ] **Running CTWA ads?** → note `wa_ref_body`, `wa_ref_headline`, `wa_ref_clid`, `wa_ref_image_url` attributes needed
- [ ] **Want a Kraya WhatsApp Widget embedded on the website?** (floating WhatsApp button)
- [ ] **Meta Lead Ads delivery preference:** direct webhook (fast, recommended) or via Google Sheets?

---

## Section 3 — Qualification Logic
*Feeds: Qualification Requirements (THE core of the AI setup)*

- [ ] **What does "qualified" mean to you?** (1-sentence definition — e.g. "shared budget + timeline + location")
- [ ] **Minimum questions the AI must ask before marking qualified:**
  - Q1: _______ (exact wording + options if any)
  - Q2: _______
  - Q3: _______
  - Q4+: _______
- [ ] **Any branching logic?** (e.g. "if they choose Option A in Q1, ask X next; if Option B, ask Y")
- [ ] **Welcome message** — first message sent when a new lead is created (use `{name}` placeholder)
- [ ] **Acknowledgement message** — sent once the lead is marked qualified
- [ ] **Exit message** — sent if lead says "stop / not interested / don't message"
- [ ] **Keywords that auto-escalate to human or a specific stage** (e.g. "lawyer", "refund", "complaint")
- [ ] **Disqualification criteria** — who is NOT worth pursuing? (e.g. wrong location, underage, competitor)
- [ ] **Common objections + preferred responses** (price too high, already using competitor, not the right time)
- [ ] **Price / package / offer info the AI may share directly** (vs. "our team will get back to you")
- [ ] **Escalation triggers** — when should AI stop and hand off to a human?

---

## Section 4 — Sales Funnel Stages
*Feeds: Stages, Pipelines*

- [ ] **Beyond "New Lead" and "Qualified" (fixed), what stages does a lead pass through?**
  - Example set: Contacted → Demo Scheduled → Proposal Sent → Negotiation → Won / Lost
  - Capture: stage name + 1-line definition (AI uses the definition for AI Stage Shifting)
- [ ] **Do you need multiple pipelines?** (e.g. separate pipelines per product line / geography / sales vs. support)
- [ ] **Pipeline owner for each pipeline** — *CRITICAL:* this person's WhatsApp number must match the pipeline phone
- [ ] **Which stage is "end state" for a won deal?** Which is "lost"?
- [ ] **Should the AI be allowed to move leads beyond Qualified automatically?** (→ AI Stage Shifting decision)

---

## Section 5 — Attributes (Custom Fields)
*Feeds: Custom Attributes*

- [ ] **What data points do you need to track on each lead?** (budget, location, product interest, source campaign, lead score, preferred language, etc.)
- [ ] For each attribute, capture:
  - Name
  - Data type: text / number / date / datetime / dropdown (list options if dropdown)
  - Required or optional
  - Should the AI try to extract it from conversation and auto-fill? (y/n)
- [ ] **Limit:** 25 custom attributes on free tier — prioritize if they want more

---

## Section 6 — Sequences (Auto Followups)
*Feeds: Auto Followups*

For EACH sequence the client wants:

- [ ] **Sequence name + purpose** (e.g. "Post-qualification nurture", "Re-engagement for no-response", "Post-won onboarding", "Product education drip")
- [ ] **Trigger** — when does this sequence start? (on qualification, on stage X, on no response, manually)
- [ ] **Stop condition** — when should it stop? (lead replies, reaches stage X, sequence completes)
- [ ] **Messages in the sequence** — for each:
  - Type: WhatsApp text / WhatsApp Template / Email / Reminder
  - Content (with `{lead_name}`, `{org_name}` placeholders)
  - Timing: immediately / after X hours / after X days / at a specific time
  - Attachment? (image, PDF, video)
- [ ] **Channel note:** sequences sent via WhatsApp Extension require agent keeps WhatsApp Web open. WhatsApp Template sequences require an approved Meta template and can reopen 24hr+ conversations.
- [ ] **Email sequences:** confirm valid email list + credit consumption OK

---

## Section 7 — Smart Triggers / Automations
*Feeds: Smart Triggers (event → action rules)*

For each workflow scenario, capture the event → action mapping. Use this grid:

| Trigger (event) | Condition / filter | Action |
|---|---|---|
| Lead created in pipeline X | (e.g. source = Meta) | Start "Nurture" sequence |
| Lead moves to Qualified | — | Start sequence / assign via round-robin / notify sales |
| Sequence ends | Sequence = "Nurture" | Move to stage "Cold" |
| No response for X days | Stage = Qualified | Send template / move to Lost |
| Call logged "no response" | — | Retry sequence / set reminder |
| Keyword detected | Keywords: pricing, cost, rate | Send pricing template / move to Hot |
| Lead stays in stage for X time | Stage = Contacted, time = 3 days | Move to Stale / notify owner |

Supported trigger events (reference): lead moves to stage, sequence ends, new lead created, no response from lead (X time), keyword detected, lead stays in stage for X time, call logged.

Supported actions (reference): move to stage, start sequence, stop sequence, set call reminder, send WhatsApp template, toggle AI, toggle auto-followup, round-robin assignment.

- [ ] **Round Robin:** should leads auto-distribute across sales agents? If yes, which pipelines + in what order + any skip weights?

---

## Section 8 — WhatsApp Channel Decision
*Feeds: WhatsApp API Integration / Chrome Extension setup*

- [ ] **Which channel?** Explain trade-offs before asking:
  - **WhatsApp API (Meta Cloud API):** scales to unlimited agents, supports broadcasts & templates, reopens 24hr+ conversations with templates, requires Meta Business verification, has Meta messaging costs per conversation
  - **Chrome Extension (personal WhatsApp Web):** no Meta approval needed, uses personal/business WhatsApp number, agents must keep Chrome + WhatsApp Web open, no broadcasts at scale, risk of WhatsApp bans if misused
  - **Both:** API for broadcasts + Extension for agent conversations (common setup)
- [ ] If **API:** Meta Business Manager ready? Business verified? WABA already created? Display name finalized? Payment method on Meta Business?
- [ ] If **Extension:** how many agents? Each has Chrome + stable internet? Phones match pipeline owner phones?
- [ ] **Phone number(s) to connect** — confirm each
- [ ] **Existing approved WhatsApp templates to migrate?** Paste content + template name
- [ ] **Quick Replies** — list common saved messages each agent uses (initial greeting, pricing, location, calendar link, thank you)
- [ ] **Record the channel decision in Section 10** with the same scope / owner / target date as any other integration. A client asking to move to WhatsApp API is an integration request, not a conversation — if it is left in prose it does not get built.

---

## Section 9 — Team Structure
*Feeds: Teams, User Pipelines, Round Robin*

- [ ] **How many users on the account?**
- [ ] **For each user:**
  - Name
  - Email (login)
  - Phone number (must match pipeline phone if they own one)
  - Role: Admin / Member
  - Which pipelines do they access?
  - Should AI Auto-Reply / AI Stage Shifting / Auto Followup be ON by default? (can override per-lead later)
- [ ] **Round Robin config** — user order and skip weights (see Section 7)

---

## Section 10 — Integrations to Wire Up
*Feeds: Integration pages*

**Cross-check Section 2 before you start.** Every lead source ticked in Section 2 that Kraya can integrate with MUST appear below with an explicit decision — a source named as live but never wired is the most common setup miss, and it also makes the AI describe a channel the account cannot receive on.

Tick + capture credentials/URLs for each, and record a decision on every ticked line: **in scope for this build / deferred**, an owner, and a target date. A ticked box with no decision is not captured.

- [ ] **Zoho CRM** — OAuth creds, sync direction (1-way push / 1-way pull / 2-way), custom field mapping
- [ ] **GoHighLevel** — API key, sync frequency
- [ ] **Google Sheets** — sheet URL, tab name, column → field mapping
- [ ] **IndiaMART** — account access, webhook URL to paste
- [ ] **JustDial** — account linking
- [ ] **Meta Lead Ads** — Business Manager access, Page Access Tokens, webhook + verify token, form field mapping
- [ ] **Meta Conversions API (CAPI)** — Pixel ID, stage → event mapping (e.g. Qualified → Lead, Won → Purchase)
- [ ] **NeoDove** — credentials, country code
- [ ] **Fireflies** — API key, webhook URL, ops team email for call analysis reports
- [ ] **MyOperator / other telephony** — creds + webhook
- [ ] **Custom API (Connect Kraya)** — generate slug + API key for their backend team
- [ ] **Inbound webhooks** — any custom system pushing leads? URL + secret
- [ ] **Outbound webhooks** — any external system to notify on Kraya events? URLs + events

---

## Section 11 — Knowledge Base Inputs
*Feeds: Knowledge Base, FAQs, Sendable Assets*

- [ ] **Collect these files:**
  - [ ] Product brochures (PDF)
  - [ ] Pricing sheets
  - [ ] Service catalogues
  - [ ] Website URL(s)
  - [ ] Past chat transcripts (if any, for training tone)
  - [ ] Case studies, testimonials
- [ ] **FAQ list** — common questions leads ask + the preferred answer. Format:
  - Category Title → Category Description → Article Title (question) → Article Content (answer)
  - Target 15–30 FAQ entries for a strong KB
- [ ] **Sendable files the AI can share directly with leads** (pricing PDF, case study, calendar link) — note each + plan tier limit (free=2, basic/pro=20)

---

## Section 12 — Subscription, Credits, Billing
*Feeds: Pricing / billing config*

- [ ] **Plan tier:** Free (trial 3 days) / Basic (auto-followups) / Pro (AI + credits)
- [ ] **Monthly AI reply estimate** → size credit pack (each reply + qualification summary consumes credits)
- [ ] **Email sends per month** (if using email sequences — consumes email credits)
- [ ] **Billing contact + payment method confirmed**

---

## Section 13 — Success Criteria & KPIs

- [ ] **What does success look like at 30 / 60 / 90 days?** (e.g. "30% qualification rate", "20 qualified leads/day", "5% conversion to won")
- [ ] **KPIs to track in analytics:** qualified leads/day, response rate, sequence completion, conversion to won
- [ ] **Reporting cadence + audience** (weekly email to founder? dashboard access for manager?)

---

## Section 14 — Commitments Made on This Call
*Feeds: Ops CRM tasks (`api-reference.md` §23)*

**Everything the Kraya rep promises on the call is a commitment, including the ones that sound like small talk.** "We'll have you live in three days", "we'll get your old contacts in", "I'll walk your team through it" — these are what the client remembers and measures the account by, and they are the ones that vanish because nobody wrote them down. A commitment left in the transcript is a commitment not delivered.

Record each one with:

- [ ] **The promise, verbatim** — the rep's own words, not a paraphrase
- [ ] **The date it was made** (the call date)
- [ ] **The deadline promised**, or `none given` — never invent one
- [ ] **Who owns it** — the POC unless the rep named someone else

Categories to sweep the transcript for:

- [ ] **Setup timeline** — "live by Friday", "three days for the whole thing", any date the client will hold us to
- [ ] **Integrations** — anything agreed here must ALSO appear in Section 10 with its decision
- [ ] **Data imports** — existing contacts, past leads, a spreadsheet they will send, a CRM export
- [ ] **Channel migrations** — moving to WhatsApp API, adding a second number, switching a number between agents (also Section 8)
- [ ] **Training / demo sessions** — a walkthrough for their team, a follow-up call, a recorded demo
- [ ] **Anything the rep said "we'll" or "I'll" about** — the catch-all; when in doubt, record it

A commitment we cannot keep is not deleted from this list; it is recorded and then explicitly renegotiated with the client.

---

## End-of-Call Output Checklist

Before ending the call, confirm you have enough to produce:

- [ ] **Qualification Requirements** block (Rules + Welcome + Q1–QN with branches + Acknowledgement + Qualification criteria + Exit message)
- [ ] **Org Information** paragraph
- [ ] **Knowledge Base** files + URLs collected
- [ ] **FAQ CSV** (Category / Description / Question / Answer)
- [ ] **Stages list** (name + description per stage)
- [ ] **Pipelines list** (name + owner + phone)
- [ ] **Attributes list** (name + type + required? + AI-inferred?)
- [ ] **Sequences** (each: name, trigger, messages with channel + content + timing + attachments, stop condition)
- [ ] **Smart Triggers grid** (event → condition → action)
- [ ] **WhatsApp channel decision** + prerequisites confirmed
- [ ] **Team roster** (name, email, phone, role, pipelines, default toggles)
- [ ] **Integrations list** (which + creds / URLs / mapping + in scope / deferred, owner, target date)
- [ ] **Commitments list** (Section 14: each promise verbatim, its date, its deadline, its owner)
- [ ] **Plan + credit estimate** agreed
- [ ] **Success criteria** recorded

---

## Post-Call Actions (Ops Member)

- [ ] **Write every Section 14 commitment to the client's Ops CRM card as a task**, and every in-scope Section 10 integration as an integration request — one POST to `api-reference.md` §23. This is what makes the promises survive the call; the account-setup agent does it automatically when it analyses the transcript, so check the card rather than duplicating them by hand
- [ ] File the Fireflies transcript + auto-generated KB draft
- [ ] Clone template account if one matches; otherwise build from scratch
- [ ] Pre-populate all 9 config areas BEFORE the onboarding call (client should walk into a ready account)
- [ ] Test one end-to-end lead in staging before onboarding call
- [ ] Share a 1-page "Your Kraya setup" summary with the client + the onboarding call invite
