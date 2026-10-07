# How Kraya accounts are set up today (Agent Hub reference)

This documents the pipeline the ops team runs in Agent Hub, so the cloud agent can reproduce it end to end over the API. Sources: `agent-hub/src/lib/ops/**`, `agent-hub/src/lib/ops/kraya-builder/**`, `Kraya-Laravel`, `Kraya-LLM/templates`.

## 1. The shape of the process

```
Brainstorming call (ops + client)  ──►  Ops client record in Agent Hub
   Fireflies transcript, checklist,        industry (picked or auto-detected)
   files, website, preferences             8-question preference questionnaire
                │
                ▼
   Five chat agents run in order, each fed the client context + the outputs it needs
   1 client-profile-builder        → Client Profile (facts, numbers, objections, handoff, gate type)
   2 ai-qualification-builder      → Qualification Requirements + About + FAQs + Builder Notes
   3 sequence-outline-generator    → per-sequence day plan with one concrete asset per day
   4 sequence-writer               → full WhatsApp copy per message
   5 account-setup-builder         → stages, triggers, attributes, quick replies
                │
                ▼
   Kraya build (9 API steps)  login/signup → metadata → attributes → stages → sequences
                              → org_info → faqs → rules → quick_replies
   Each step: an LLM "extractor" turns the relevant agent output into the exact API payload,
   deterministic sanitizers clean it, the API is called item by item, IDs are recorded.
                │
                ▼
   Account Updates agent (conversational) for every change after the build:
   discover → propose → confirm → mutate, one entity per turn, "confirm delete" for deletes.
```

The cloud agent collapses the five agents and the build into one conversation with the ops rep, but the phases, the artefacts, and the rules stay the same.

## 2. Inputs the process starts from

| Input | Where it comes from | What it feeds |
|---|---|---|
| Call transcript (required) | Fireflies recording of the brainstorming call | every agent; the single most important source of real numbers, objections, language |
| Brainstorming checklist | ops fills `brainstorming-checklist.md` on the call; uploaded as a tagged file | qualification, stages, attributes, sequences, triggers |
| Website URL and documents | brochures, price lists, catalogues; scraped/extracted to text | Client Profile, FAQs (primary grounding), knowledge-base attachments, sendable files |
| Sales notes / win details / ops notes | CRM record for the deal | Client Profile |
| Industry | picked at intake from the nine playbook keys, else detected from the profile | industry playbook chunks injected into each generation step |
| Preferences | 8 closed questions (see `content-rules.md` §6) | qualification flow style, sequence and FAQ tone |

Context is assembled in this order and trimmed from the bottom when over budget: CLIENT INFO, OPS NOTES, BRAINSTORMING CHECKLIST, WIN DETAILS, SALES NOTES, PREVIOUS AGENT OUTPUTS, CALL HISTORY, CALL TRANSCRIPTS, INDUSTRY REFERENCE, CLIENT FILES. The checklist sits high on purpose: it is the ops rep's structured answers and must never be squeezed out by a brochure.

Which upstream outputs each agent receives (`AGENT_INPUT_NEEDS`):

| Agent | Receives |
|---|---|
| client-profile-builder | nothing upstream |
| ai-qualification-builder | Client Profile |
| sequence-outline-generator | Client Profile |
| sequence-writer | Client Profile, Sequence Outline |
| account-setup-builder | all four |

## 3. The five agents

Full system prompts are in `agent-prompts/`. Kickoff (first user turn) prompts are in `agent-kickoffs.md`. One-paragraph summary of each:

1. **Client Profile Builder** extracts facts only. Its five high-leverage sections decide the quality of everything downstream: hard-numbers inventory (exact figures with currency), testimonials with proper nouns, verbatim objections, human-handoff contacts, and the qualification gate type (volume/fit, deliverable-inputs, profile+intent, routing). It also settles the customer language and the `bot_languages` value. Anything missing is a Gap, never an invention.
2. **AI Qualification Builder** writes the AI's brain: a numbered Rules block (15 standard flow and robustness rules plus client rules), a welcome instruction, 3–7 instructional questions each mapped to a named attribute and marked Required/Optional with free-text equivalents, an Attribute & State Mapping (question attributes plus workflow-state attributes with enumerated values, and the rule that attributes are read-only to the AI), a Stage Shifting table, numbered Edge Cases, the acknowledgement message, and an explicit Qualification section. It also produces the About block and grounded FAQs. Question format is instructional unless the client asked for verbatim wording.
3. **Sequence Outline Generator** designs 5 core sequences (DNP/No-Response, Interested Nurture, Lead Lost/Dormant Revival, Validation, Call Booked) plus optional Promotional and No-Show Recovery, sized by sales cycle, with one concrete asset per day, an interlock/routing table aligned to Kraya's automation chain, and a `<Segment> - <Purpose>` name per sequence.
4. **Sequence Writer** writes every message against the "earn its send" rules: one asset per message, quantify or delete, CTA names its payoff, objections answered with mechanics, message 1 asks for the smallest reply, urgency only with a verifiable mechanism, social proof only with a proper noun, final message captures Interested/Later/Stop, never presuppose a reply. Hard publish gate: no placeholders, no ops notes, no draft artefacts.
5. **Account Setup Builder** produces the stage list with AI on/off per stage, the 7 core smart triggers plus one stage → sequence rule per pair, the attribute list reproduced exactly from the qualification's state mapping with a description on every attribute, and 15–20 quick replies in 6 groups.

## 4. The build: agent output → API payload

For each step the builder feeds the extractor: the endpoint's description and a rich example (`content-rules.md` §2), the relevant agent output, the industry playbook chunk for that step, and the client's preference block. The extractor returns a typed payload; deterministic code then enforces what prompts alone failed to enforce in production.

| Step | Source output | Deterministic guards applied before the API call |
|---|---|---|
| attributes | account-setup-builder | key sanitised to `[A-Za-z0-9 _./&-]`; dropdown without values skipped; "already exists" reused |
| stages | account-setup-builder | default stage names filtered out; `order` renumbered to start strictly after the live Qualified order; 409 → reuse existing id |
| sequences | sequence-writer + outline | purpose label stripped, emoji stripped (Agent Hub strips unconditionally; the cloud agent strips only when the client did not ask for emoji), dashes → commas, `{lead_first_name}` only, placeholder gate drops the message, duplicate name → " (Agent Hub)" suffix, delay `{value,unit}` → Kraya schedule |
| org_info | ai-qualification-builder | dash sanitised and emoji stripped (same client-choice caveat), placeholder URLs (`example.com`, bare "TBD") dropped from attachments, `attachments` JSON-encoded, `bot_languages` always sent (default English) |
| faqs | client docs (primary) + profile + qualification | content sanitised; dedupe on `category::title` |
| rules | account-setup-builder + stored ids | core rule set built in code from real ids; extracted rules that duplicate a core rule dropped; fabricated ids scrubbed and re-bound by name; rules still missing required fields skipped, not sent |
| quick_replies | account-setup-builder (+ profile) | placeholder gate; server-side dedupe by name; key = slugified name |

Extraction uses a small model for structure and a strong model for the two prose-critical steps (org_info and sequence copy). The cloud agent authors these directly, so it must apply the same guards itself (they are listed as checklists in `content-rules.md`).

## 5. What the runtime does with the configuration

The Kraya LLM service renders the account into the qualification prompt on every inbound message. New orgs use the `qualification-v4` template; older orgs may still be on `qualification-v3.3`. What matters for authoring:

- `org_name` and `about` are injected as "Company Name / About the Company". `about` is the AI's only persistent memory of the business, so every non-negotiable fact lives there.
- `qualification_requirements` is injected verbatim as the operating spec. The prompt tells the model: treat it as free text written by the business (templates, question sequences, branching, skip rules, file triggers, stage triggers, exit rules); when it gives exact wording, use it closely; when it conflicts with the base prompt, the org spec wins except the repetition gate and hard rules. That is why the structure (Rules, Welcome, Questions with attribute mapping, State Mapping, Stage Shifting, Edge Cases, Acknowledgement, Qualification) is load-bearing.
- `bot_languages` becomes a hard `<language_constraint>` block listing allowed languages; Hindi, Hinglish and English are treated as three separate languages. An explicit language instruction inside `qualification_requirements` overrides the list. With no value set, the runtime defaults to English regardless of the lead's language.
- Custom attributes are presented as an "Attributes to Extract" table: key, format hint (freeform, numeric, `YYYY-MM-DD`, dropdown one of (...)), and the description when present. Descriptions are how the extractor knows what to fill.
- Stages for `change_stage` come from `stages_information`: every non-inbox stage of the Leads pipeline with its `id`, `name` and `description`. The model must match a stage rule from `qualification_requirements` or a stage description; it is told to prefer the most specific downstream stage and to return null when unsure. Stage descriptions therefore matter, and the Stage Shifting table must use the exact stage names.
- `sendable_files` is offered as a JSON list of `{id, name, type, description}`; the model can only send a file that is in the list and only on an org-defined trigger or a direct request. The description is the send trigger.
- FAQs and knowledge-base chunks are retrieved by similarity (top-k, default 5) and injected as `<faqs>` and `<kb_articles>`. Attachments in org info are what get scraped into the knowledge base.
- The runtime enforces its own style: one fresh question per reply, no invented facts, no "great question" openers, the lead's name at most every 4–5 messages, a repetition gate over the last 3–4 bot messages, and a `flow_state` ledger so answered questions are never re-asked.

## 6. After the build

The Account Updates agent handles every later change through the same API with the safety protocol in `api-reference.md` §21. Reps commonly ask for: message rewrites, added FAQs, new stages, rule tweaks, template creation, and toggling AI on specific stages. Each is a read → merge → write, because most Kraya upserts replace the whole field they receive. Sequence messages are the exception: they are upserted by id and an update never deletes one (`api-reference.md` §8).

## 7. Production learnings that shaped the rules

From the 142-org analysis (`prod-account-patterns.md`): the accounts that actually use AI share a qualification config of 8–20k characters with the full spine; described dropdown attributes are the marker of a deliberate setup; the silence → No Response → re-engagement chain carries most rule executions; keyword → Qualified rules mostly never fire; 71% of orgs never touched the 4 seeded quick replies; placeholders shipped to real leads in 7 accounts, which is why the publish gate exists.
