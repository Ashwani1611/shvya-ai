# Conflict audit: finding configuration that makes the AI hallucinate

Most invented answers on Kraya accounts are not model failures. They come from the account's own configuration: an instruction that asks the bot for something the account never supplied, or two sources that state the same fact differently. This file is the procedure for finding those before go-live, on demand for an existing account, and when a rep reports an invented answer.

## Why configuration causes hallucination

The qualification prompt reads `about`, `qualification_requirements`, `bot_languages`, `sendable_files`, the stage list with descriptions, the lead's attributes, the retrieved FAQs and knowledge-base chunks, and the conversation so far, all in one turn. `qualification_requirements` is treated as the business's operating spec and wins over the base prompt's general rules. Three things follow.

1. **A promise without material gets filled.** September 2026, SKY HOMES and Leaders for India: the spec told the bot to answer RERA, location and event questions and its templates promised floor plans and "upcoming events", but none of that was in the account. The model supplied it from adjacent real data (a RERA number ending `…156` became `…157`, an FAQ about a "Business Shower" became a "business showcase"). The base prompt already said "never invent facts" and lost to the concrete org instruction. The platform now has a hard rule that URLs, ids, addresses, dates, venues and names must appear verbatim in org context, but a spec that explicitly asks for an answer still pulls against it. Do not rely on the base prompt; remove the pull.
2. **Contradictory sources get blended.** When `about` says one phone number and an FAQ says another, or `about` quotes ₹25,000 and a retrieved FAQ says "from ₹20,000", the model picks one per turn, or mixes them. Which FAQs are retrieved changes with the lead's wording, so the answer changes between leads.
3. **Fabrications compound.** Once the bot states an invented fact, it sits in the conversation, and the internal conversation summary records bot claims as facts about the lead. Later turns treat it as established. Preventing the first invented turn prevents the chain; fixing the config later does not clean summaries already written for existing leads.

Messages the business sends also sit in the conversation the bot reads. A sequence, broadcast, template or quick reply that mentions an offer, an event or a catalogue tells the bot that thing was offered, and leads reply asking for it.

## 1. Gather every source (delegate to a subagent)

Read-only calls, returned as compact JSON with the source location of every piece of text:

| Source | Call | Locator to keep |
|---|---|---|
| `about`, `qualification_requirements`, `bot_languages`, `attachments`, `sendable_files` | `GET /users/metadata` → `user.organization.info` | field name + section heading or rule number |
| Stages (name, description, `ai_switch`) | `GET /users/metadata` → `pipelines[].stages[]` | stage id |
| Attributes (key, type, values, description) | `GET /users/metadata` → `attributes[]` or `GET /custom-attributes` | attribute key |
| FAQs | `GET /faqs/articles?page=1&count=1000` | article id + title |
| Sequences and messages | `GET /auto-responder/sequences` | sequence name + message order |
| Quick replies | `GET /quick-replies` | group + reply name |
| Bump-up steps | `GET /users/metadata` → `fixed_bump_ups`, `bump_up_steps[]` | step index |
| WhatsApp templates (API accounts) | `GET /whatsapp/templates?count=1000` | template id + display name |
| Knowledge-base attachments | the `attachments[].url` list; fetch the pages if you have a web tool, otherwise record "not verified" | URL |
| Client materials, when available | transcript, checklist, profile, documents | file + line |

Seeded onboarding content counts: an account that was never cleaned still carries industry template org info, FAQs and sequences (`industry-templates.md`) that describe a generic business.

## 2. Run the checks

### A. Fact ledger (contradictions)

Extract every checkable fact from every source into one table: phone numbers, emails, URLs, addresses and locations, prices, fees, discounts and offer terms, MOQs, business hours, batch or event dates, durations, counts ("28 branches"), people's names and roles, product, service and program names, eligibility rules. Normalise before comparing: phone digits with country code, `₹`/`INR`/`Rs`, `10k`/`10,000`, date formats, casing.

Flag any fact with more than one value. Also flag near-duplicates the model can confuse: two numbers that differ by one digit, two similar program names, two branches with similar addresses.

### B. Promise without material (highest risk)

List every sentence in any source that commits the bot or the business to answer, share, send, confirm or describe something. Typical verbs and nouns: answer / share / send / provide / give / tell them about / confirm / book; brochure, catalogue, price list, floor plan, RERA, location pin, map link, payment link, UPI, video, demo, schedule, upcoming events, batch dates, availability, slot, offer, discount.

For each one, locate the material the bot can actually reach:

| Promise | Counts as material |
|---|---|
| a fact (price, RERA, date, venue, address, URL, name) | the exact value written in `about`, `qualification_requirements`, an FAQ answer, or a verified attachment page |
| a file | a `sendable_files` entry whose `name` and `description` match, referenced in the spec by its UUID next to its name (a file *name* where the model expects an id makes the LLM service fail) |
| a booking or confirmation | a real mechanism: the calendar booking link, a human hand-off rule, or a stage the bot can move the lead to. The bot cannot confirm a slot on its own |
| a link or payment | the literal URL or UPI id in org context. The bot must never compose one |

No material means a finding. Resolve it one of two ways, chosen by the rep: supply the material (add the value to `about` or an FAQ, upload the file as a sendable file), or rewrite the instruction into a hand-off ("the team will share the floor plan on the call").

### C. Instruction conflicts inside the spec

Read `qualification_requirements` as a whole and flag rule pairs that cannot both hold:

- collection vs answering: "collect details first" and "answer questions first"; "never discuss pricing" and "ask for budget" or an FAQ that states prices
- retry rules: "one clarification per question" and "keep asking until answered"; a universal skip rule and a question marked mandatory
- qualification criteria: the `## Qualification` section, the Stage Shifting table and the question list disagree on which answers qualify a lead
- required vs optional: a question marked Optional in one place and required in another
- duplicated messages: two different welcome, acknowledgement or exit texts
- numbering: two rules with the same number and different content, or a rule referring to a question number that no longer exists
- language: a language instruction in the spec that disagrees with `bot_languages` (the spec's explicit instruction wins at runtime, so the field is misleading)
- identity: one place says the assistant is a named person, another says it is an assistant for the company, or a persona name differs between `about` and the welcome message

### D. Spec vs platform and preferences

- the pricing preference is `defer` while `about` or an FAQ states prices (retrieved FAQs win in practice)
- the spec tells the bot to do something the platform does not do: call the lead, take payment, send a file that is not in `sendable_files`, show buttons the channel cannot render, confirm a booking without a hand-off
- Stage Shifting names a stage that does not exist, or with different wording or casing
- the Attribute & State Mapping names an attribute key or dropdown value that does not exist on the account
- a stage description contradicts the Stage Shifting row that targets it

### E. Stale and leftover content

- seeded industry org info, FAQs or sequences that describe a different business ("healthcare provider" boilerplate on a dental clinic, the education template's sample academy)
- dates, batches, offers or deadlines already in the past
- FAQs about products, branches or offers the client no longer mentions in its current materials
- placeholder or dead URLs in attachments (`example.com`, pages that 404)

### F. Sources and channels named as live but not wired

`org_info`, FAQs and sequence copy must not present a lead source, channel or integration as active unless it actually exists on the account. Check every source named in the About block against the integration tables and the connected numbers. A config that tells the AI "our lead sources are Meta ads, IndiaMART and TradeIndia" while only click-to-WhatsApp is connected will have the bot reference channels the client cannot receive on, and hides the unbuilt integration from everyone reading the account.

### G. Lead-facing copy that primes unanswerable questions

For every sequence message, quick reply, bump-up step, template and recent broadcast: if it mentions a fact, offer, event, file or link, check that the bot can answer the follow-up question from material found in B. A nurture message saying "our new batch starts soon" with no batch date anywhere in org context will produce invented dates.

## 3. Report

One table, most severe first. Quote at most 25 words per source.

| Severity | Type | Where (field + locator) | Text | Why it causes invented answers | Proposed fix |
|---|---|---|---|---|---|
| Block | Promise without material | `qualification_requirements` rule 7 | "Share the floor plan and RERA details when asked" | no floor plan file, no RERA number anywhere | ask client for the RERA number and floor plan PDF, or change to "the team will share RERA and floor plans on the call" |
| Block | Contradiction | `about` ¶3 vs FAQ #4412 | "+91 98xxx 43210" vs "+91 98xxx 43201" | two phone numbers one digit apart | rep confirms the real number; fix the other source |
| Fix before go-live | Instruction conflict | rule 4 vs rule 11 | "never discuss fees" / "share fee ranges when asked" | both can't hold | keep one per the pricing preference |
| Note | Stale | FAQ #4420 | "Diwali offer valid till 5 Nov 2025" | expired offer | delete or update |

Severity:
- **Block**: a promise without material, a contradiction in a fact the bot states to leads (price, phone, address, date, link, legal id), a Stage Shifting or attribute name that does not exist. Handover does not proceed until the rep resolves every Block finding.
- **Fix before go-live**: instruction conflicts, spec vs preference conflicts, stale offers and dates.
- **Note**: near-duplicates with no current contradiction, style inconsistencies, leftovers that do not state facts.

Never choose the true value yourself when two sources disagree. The rep or the client decides; then fix every other source through the normal propose, confirm, mutate flow. Keep one canonical home per fact (usually `about`, with FAQs quoting it verbatim) so the next edit changes one place.

## 4. Probe with the demo chat

Static reading misses what retrieval surfaces. After fixing, probe the live account with `POST /demo/ai-chat/generate-reply` (`api-reference.md` §18):

- one lead message per promise from check B and per fact from check A, asked directly ("Can you share the RERA number?", "What is your address?", "When is the next batch?")
- one message per resolved contradiction, phrased the way a lead would, so the retrieved FAQ is the one that used to conflict
- pass: the reply contains the exact value from org context, or a hand-off. Fail: any value not verbatim in org context, or the old value from a source you changed

Run each probe 3 times before go-live. When verifying a fix for a reported hallucination, run the probe 10 times on identical inputs and require at least 9 passes; report the exact count. The demo chat does not use a lead's stored summary or attributes, so it tests first-turn behaviour only.

## 5. When a rep reports an invented answer

1. Find the invented value in the trace output (`langsmith.md`).
2. Search the same trace's inputs for the instruction that asked for it (check B). This is the usual cause.
3. Search the inputs for adjacent real data it anchored on: a near-identical number, a similarly named program, a neighbouring branch.
4. Check whether an earlier bot message or the conversation summary already carried the value. If it did, the lead's stored summary will keep repeating it after the config is fixed; flag the lead to ops.
5. Fix the configuration (supply material or convert to a hand-off). When the spec must keep answering that category, add a checkable rule to `qualification_requirements`, for example: "State RERA numbers, dates, venues, addresses and links only exactly as written in the company information. If a value is not there, say the team will share it."
6. Verify with the 10-run demo-chat probe (at least 9 of 10), then run the full audit on the account, because the same pattern usually exists elsewhere in it.
