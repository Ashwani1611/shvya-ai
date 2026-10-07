# Vault sections ↔ brainstorming call ↔ Kraya

Use the `key` column with `vault.py note --section <key>`. "Call §" is the section of `kraya-account-setup/references/brainstorming-checklist.md` where the fact is usually discussed.

| key | Section (what the client sees) | What belongs here | Call § | Lands in Kraya as |
|---|---|---|---|---|
| `website` | Website pages | One URL per key page (home, services, pricing, about, contact) | 11 | `org_info.attachments` type `url` |
| `brochures` | Brochures, catalogues & price lists | PDF / DOCX / XLSX the team already shares | 11 | `org_info.attachments` type `file` |
| `media` | Photos & videos the AI can send | Files to send mid-chat + "what it is / when to send it" | 11 | `org_info.sendable_files` (name, type, description) |
| `offerings` | Products, services & pricing | Full list, prices or ranges, packages, **pricing disclosure choice** (exact / ranges / on call) | 1, 3 | `about` Core Services, FAQs, `pricingDisclosure` rule |
| `basics` | Business basics | Branches ("one branch only" counts), hours + timezone, phone/email/address, booking and payment links | 1 | `about`, `auto_responder_hours`, Info & Links quick replies |
| `faqs` | Questions leads ask & objections | Real lead questions, objections in the lead's words, the client's preferred answers | 3, 11 | `POST /faqs/articles`, objection quick replies, sequence copy |
| `team` | Who handles leads | Names, roles, numbers, who takes which lead type, escalation order | 9 | Spec escalation rules, acknowledgement message |
| `qualification` | Qualification | What "qualified" means, deal-breakers, the 3–7 questions with options, verbatim wording if required, sales-cycle length | 3 | Spec `## Qualification` + `## Qualification Questions`, custom attributes |
| `handoff` | When the AI should hand over to a human | Situations that end the AI's turn: upset customer, complaint/refund, custom quote or negotiation, asks for a person, ready to pay; what the AI says while handing over; who takes over | 3, 9 | Spec Edge Cases + escalation rules, acknowledgement message, stages with `ai_switch` off, rules that notify the team |
| `blacklist` | Topics the AI must never answer | Topics to refuse and flag: prices the client won't quote on chat, legal/medical/financial advice, competitors, internal matters | 1, 3 | `about` Safety & Guardrails + spec Rules (refuse, say the team will help, flag) |
| `rules` | Tone, language & promises | Tone, emoji yes/no, languages + script, persona name, promises it must never make (dates, results, guarantees, discounts) | 1, 3 | `about` Tone Notes, `bot_languages`, `<client_preferences>`, spec Rules |
| `proof` | Testimonials & proof | Quotes with name + city/company, case studies, numbers with units | 11 | Sequence proof messages, quick replies |
| `offers` | Current offers | Offer + end date | 11 | `about`, FAQs (never quote an expired one) |
| `scripts` | Sales scripts & past chats | Chat exports, call scripts, email templates | 11 | Tone only (Client Profile), nothing stored in Kraya |
| `other` | Anything else | Whatever doesn't fit; also the default section for questions | — | — |

## What the call usually gives you, by section

- **`qualification`** — almost always. The call is where the ops rep and client agree on what to ask and whom to drop. Write the agreed questions as a numbered list in the client's words.
- **`rules`** — usually. Tone, language mix and script, persona name, emoji, never-promise items.
- **`handoff`** — usually. Listen for "then don't ask anything else", "send them to reception", "my team calls": every moment the client says a human takes over.
- **`blacklist`** — usually. Anything the client says the AI must not answer at all (not just phrase carefully): write the topic and that it should be flagged to the team.
- **`basics`**, **`offerings`** — often partial. Write what was said, then `ask` for the rest (e.g. exact hours, a price sheet).
- **`team`** — often names without numbers. Write the names, ask for numbers.
- **`faqs`**, **`proof`**, **`offers`** — sometimes mentioned in passing. Write one note per section only if there is a concrete fact; do not pad.
- **`website`**, **`brochures`**, **`media`** — the call rarely gives files. Do not write notes here from a transcript; the client uploads. If the rep pasted links, add them as a note in `website` listing the URLs.

## Not for the Vault

Ops-internal material stays out: pack, credits, billing, KPIs (call §12–13), WhatsApp channel decisions (§8), integrations (§10), and anything about other clients. The client reads every note you write.
