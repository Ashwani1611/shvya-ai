# Kraya Account Agent — API Reference

This is the API contract for an agent that configures a Kraya account by talking to the operations team. It covers the endpoints the Agent Hub account-setup builder and account-updates agent use today, plus the account-level settings a fully working account needs (bump-ups, uploads, WhatsApp API account settings, calendar, Co-Pilot). Every request shape is verified against the Laravel validators.

**Assumptions.** Ops provides an **existing** account that is already on a paid pack (Basic or Pro) with onboarding completed, plus its admin login email and password. The agent never signs up, never changes the pack, never adds team members (it may configure existing members' settings), and never connects a WhatsApp API or Instagram account. Configuring an *already connected* WhatsApp API account is in scope.

Sources of truth, in order: Laravel `Validator::make` rules in `Kraya-Laravel/app/Http/Controllers/*.php`, then this doc. Nothing here is inferred.

---

## 1. Basics

| Item | Value |
|---|---|
| Base URL | `https://api.kraya-ai.com/api` (staging: `https://api-staging.kraya-ai.com/api`) |
| Auth | `Authorization: Bearer <token>` on every call except `POST /auth/login` |
| Content type | `Content-Type: application/json` for every non-GET call, except the two multipart uploads (`POST /upload`, `POST /calendar/settings/logo`) |
| Token source | `token` field of the login response. Tokens are Passport personal-access tokens; on a `401`, re-run `POST /auth/login` and replay the request once |
| Role | Log in as an org `admin`. Sequences, org info, pipelines, rules, stages, templates, bump-ups, calendar and WhatsApp account settings are behind `admin-only` middleware |
| Pack | Calendar is Basic/Pro only. WhatsApp API features need a connected account. Free-tier caps (5 sequences, 10 attributes, 10 quick replies, 20 stages) do not apply on a paid pack |

### Response envelopes

Kraya is inconsistent. Read these exactly:

| Endpoint family | Success shape | Error shape |
|---|---|---|
| auth, metadata, upload | flat object (`token`, `user`, `pipelines`, `url`, …) | `{ message, errors }` |
| stages, pipelines, faqs | `{ data: [...] }` on lists; created entity as a flat object on create | `{ message, error }` |
| sequences, rules, templates, whatsapp accounts, calendar, bump-ups | `{ success, message, data }` (bump-ups: `organization`) | `{ success:false, message, error|errors }` |
| custom attributes | `{ success, message, attributes: [...] }` | `{ success:false, message, errors }` |
| quick replies | list is a **nested object** keyed by group name then reply key (see §9); create returns the reply | `{ message, error }` |

Status codes: `400` validation, `401` bad/expired token, `403` not admin or pipeline not yours, `404` referenced entity missing (or belongs to another org), `409` duplicate (stage slug, calendar reminder sequence), `422` inactive WhatsApp account, `500` server error.

---

## 2. Two flows

### Flow A — configure a provided account end to end (order matters)

IDs created earlier are referenced later, so run in this order. Every step is idempotent if you read first and skip what exists.

| Step | Call | Capture / notes |
|---|---|---|
| 1 | `POST /auth/login` | `token`, `user.organization.id`, leads `pipelines[].id`, every default stage id by `slug` |
| 2 | `GET /users/metadata` | current org info, attributes, stages, bump-up config, calendar flag, `has_whatsapp_business_account` |
| 3 | `POST /custom-attributes` × N | attribute `key`s and numeric `id`s |
| 4 | `POST /stages` × N (and `POST /stages` with `id` on seeded ones) | stage ids by name; every stage described, AI flag set, colour set |
| 5 | `GET /whatsapp/accounts` → `POST /whatsapp/accounts` → `POST /whatsapp/template` × N | only when an API account is connected; wait for `APPROVED` before steps 6, 9, 11 reference templates |
| 6 | `POST /auto-responder/sequence` × N | sequence ids (`data.id`) |
| 7 | `POST /upload` × N → `POST /organizations/info` | brochure / price-list URLs go into `attachments` and `sendable_files` |
| 8 | `POST /faqs/articles` × N | article ids |
| 9 | `POST /rules` × N | rule ids (`data.id`) |
| 10 | `POST /quick-replies` × N | — |
| 11 | `POST /organizations/bumpups` | fixed or AI bump-ups. **Staging only** until the bump-up release reaches production; on prod this returns 404, report it and move on |
| 12 | `PUT /calendar/settings` (+ reminder sequence) | only for appointment-driven businesses |
| 13 | `PATCH /organization-config` | Co-Pilot thresholds and stage scoping |
| 14 | `GET /users/settings` → `POST /users/settings` | per-user AI / auto-responder switches for extension and WAHA numbers, email from-name |

### Flow B — change an existing account

Log in, then for every change: **discover → propose → confirm → mutate**. Section 21 has the full operating protocol. Most mutation endpoints are upserts that **replace** the fields you send (sequence messages are the exception, see §8), so always read first.

---

## 3. Hard rules (learned in production)

1. **Upserts key on `id` being present, not non-null.** Laravel's `$request->has('id')` is true for `null`. Never send `"id": null` — omit the key to create.
2. **Most upserts replace the whole field they receive.** `values[]` on a dropdown attribute, `trigger_conditions[]` / `attribute_conditions[]` on a rule, and `org_name` + `about` + `qualification_requirements` + `attachments` on org info. To change one item, fetch, merge, send the full list back. **Sequence `messages[]` is the exception:** each entry is upserted by `id`, entries without `id` are created, and messages you leave out are kept, never deleted. Remove messages with `DELETE /auto-responder/sequence-message` (§8).
3. **Custom attributes are keyed by `key` (string), never by id.** Rename via `new_key_name`.
4. **Rules update via `rule_id`, not `id`.**
5. **`POST /organizations/info` blanks `bot_languages` when the field is absent.** Always send it (default `"English"`). `attachments` must be a JSON **string**, not an array.
6. **Seeded stages are a starting point, not a contract.** Kraya seeds six inbox views (`All Chats`, `Unread Chats`, `Needs Reply`, `Groups`, `Pending Reminders`, `Queue`; `is_default: true`, orders 0–5, leave these alone) and the funnel stages `New Lead`, `Qualified`, `Nurturing`, `Good Lead`, `Lead Won`, `No Response`, `Deleted` (orders 6+, `Deleted` at 100), each with a generic description, `ai_switch: true` and colour `#000000`. Once you have decided the client's real funnel, edit the funnel stages freely: rewrite descriptions, set colours, turn `ai_switch` off where a human owns the stage (Lead Won, Deleted, Hot Lead), reorder, and delete the ones the client will never use (`DELETE /stages/{id}` works on empty non-inbox stages). Two names must stay exactly as they are: **New Lead** and **Qualified**. Laravel matches them by name and slug when it chooses the qualification prompt, auto-moves a qualified lead, and files new leads, so renaming either silently breaks qualification. Every stage that ends up on the account must carry a one-sentence `description`: the AI reads stage descriptions when deciding where to move a lead.
7. **New custom stages must have `order` strictly greater than the `Qualified` stage's `order`** read from `/users/metadata`. Do not hardcode 3; some pipelines have `Qualified` at order 6 or 7.
8. **List endpoints paginate at `count=10` by default** (`/stages`, `/faqs/articles`, `/faqs/categories`, `/whatsapp/templates`). Pass `count=1000` (templates) or read stages from `/users/metadata`, which is unpaginated.
9. **Never guess a `whatsapp_account_id`.** Read it from `GET /whatsapp/accounts` and use only an account with `status: "active"`. Kraya accepts a foreign id, saves the template, then 500s.
10. **Templates are not de-duplicated by Kraya.** Creating a template whose `display_name` exists silently makes `"<name> v2"`. Check `GET /whatsapp/templates?count=1000` first.
11. **Sequence names are unique per org.** Duplicate returns `{ success:false, message:"Sequence with this name already exists." }`. The builder retries with `" (Agent Hub)"`, `" (Agent Hub 2)"` … up to 5 times.
12. **Identical rules are rejected.** `{ message: "A rule with identical configuration already exists: <name>", error: { identical_rule: [...] } }`. Treat as skip, not failure.
13. **Attribute keys match `^[a-zA-Z0-9 _./&-]+$`**, max 150 chars. No `+`, parentheses or other symbols. Use Title Case with spaces (`Treatment Interested In`).
14. **One mutation per turn** in conversational mode. Batched large upserts intermittently produce malformed tool calls.
15. **Deletes need two confirmations**, the second being the literal phrase `confirm delete`.

---

## 4. Auth and metadata

### POST /auth/login

No auth. Body `{ "email": "...", "password": "..." }`. Returns the full metadata object (next section) plus `token` and `token_expires_at`. `401 { message: "Invalid Credentials" }` on a bad password. This is the only entry point: ops hands the agent the account's admin credentials. `POST /auth/signup` and SuperAdmin one-time login links are out of scope; the agent has no SuperAdmin access.

### GET /users/metadata

The canonical snapshot. Fields the agent needs:

```json
{
  "user": {
    "id": 8421,
    "name": "...", "email": "...", "role": "admin",
    "organization": {
      "id": "c3a1e2f0-...",          // UUID string
      "name": "Fix My Hair",
      "slug": "fix-my-hair",
      "pack": "free",
      "timezone": "Asia/Kolkata",
      "industry": "healthcare",
      "info": {                       // current org_info — read this before POST /organizations/info
        "about": "...",
        "qualification_requirements": "...",
        "attachments": [{ "url": "...", "name": "..." }],
        "bot_languages": "English",
        "sendable_files": []
      },
      "fixed_bump_ups": false,        // bump-up mode + steps (see §15)
      "bump_up_variation_enabled": false,
      "bump_up_steps": [ { "sort_index": 0, "message": "...", "delay_minutes": 60 } ],
      "calendar_config": { "enabled": false, "calendar_slug": null },
      "onboarding_completed": true
    }
  },
  "pipelines": [
    {
      "id": 901, "name": "Leads", "pipeline_type": "leads",
      "phone_number": null, "has_api_account": false,
      "stages": [
        { "id": 5490, "name": "All Chats",   "slug": "all-chats",   "order": 0,   "is_default": true,  "ai_switch": false, ... },   // + Unread Chats, Needs Reply, Groups, Pending Reminders, Queue (orders 1-5): inbox views, ignore
        { "id": 5501, "name": "New Lead",    "slug": "new-lead",    "order": 6,   "is_default": false, "is_hidden": false, "color": "#000000", "description": "New leads that are created in the CRM..." },
        { "id": 5502, "name": "Qualified",   "slug": "qualified",   "order": 7,   ... },
        { "id": 5503, "name": "Nurturing",   "slug": "nurturing",   "order": 8,   ... },
        { "id": 5504, "name": "Good Lead",   "slug": "good-lead",   "order": 9,   ... },
        { "id": 5505, "name": "Lead Won",    "slug": "lead-won",    "order": 10,  ... },
        { "id": 5506, "name": "No Response", "slug": "no-response", "order": 11,  ... },
        { "id": 5507, "name": "Deleted",     "slug": "deleted",     "order": 100, ... }
      ]
    }
  ],
  "attributes": [ { "id": 1, "key": "Name", "data_type": "text", "values": null, "description": "...", "has_linked_rules": false } ],
  "has_whatsapp_business_account": false,   // quick check before any template work
  "quick_replies": { ... }                  // same nested shape as GET /quick-replies
}
```

Pick the pipeline with `pipeline_type = "leads"` (fall back to the first). Record **every** seeded funnel stage id by slug: rules for Won/Lost/No Response need them. `is_default: true` marks the six inbox views, not the funnel stages, so identify New Lead / Qualified and the rest by slug. Custom stages must be ordered strictly after Qualified's real `order` (7 on a fresh account, but read it).

---

## 5. Pipelines

### GET /pipelines

Query `id`, `page`, `count` optional. Returns `{ data: [ { id, name, pipeline_type, order, stages: [...] } ] }`. Use `/users/metadata` instead when you only need the leads pipeline.

Pipeline create/update (`POST /pipelines`, `{ id | name, order, pipeline_owner_id?, phone_number? }`) exists but the agents do not use it; multi-pipeline accounts are set up by ops in the UI.

---

## 6. Stages

### GET /stages

Query `page`, `count` (default **10**), `id`. Returns `{ data: [...] }`. Prefer `/users/metadata` for the full list.

### POST /stages — create or update

| Field | Required | Notes |
|---|---|---|
| `pipeline_id` | **yes** | Always, even on update |
| `id` | update only | Omit to create |
| `name` | create | Slug derived; unique per pipeline |
| `description` | no | ≤255 chars. Shown to the AI when deciding stage moves |
| `color_code` | no | Hex. Kraya defaults new stages to `#000000`; the agent auto-assigns a colour on create when omitted. Blue `#2196F3` in-progress, amber `#FFA726` action-needed, green `#4CAF50` won, red `#F44336` lost, purple `#9C27B0` parked |
| `order` | no | Funnel position. **Must be > Qualified's order** for new stages. Always send it on create so `message_queue_priority_order` is auto-populated |
| `is_hidden` | no | |
| `ai_switch` | no | `true` lets the AI keep replying / auto-move leads in this stage. `false` on Hot Lead, Human Intervention, negotiation and ops-tracking stages |
| `required_attribute_keys` | no | Array of attribute keys that must be filled before a lead can enter |

```json
{
  "pipeline_id": 901,
  "name": "Consultation Booked",
  "description": "Lead has confirmed a date and time for an in-person consultation",
  "color_code": "#2196F3",
  "order": 7,
  "ai_switch": true
}
```

Response: the stage object (`id`, `name`, `slug`, `order`, `pipeline_id`, …). `409 "Stage Already Created."` when the slug exists; look the id up in `/users/metadata` and reuse it.

### DELETE /stages/{id}

Kraya refuses default stages (scoped to `is_default = 0`) and returns `400 "Stage has leads"` when leads sit in it. Move leads first.

---

## 7. Custom attributes

Attributes are org-wide lead fields the AI extracts into and rules can filter on.

### GET /custom-attributes

Returns `{ success, attributes: [ { id, key, data_type, values, description, has_linked_rules } ] }`.

### POST /custom-attributes — create or update (keyed by `key`)

| Field | Required | Notes |
|---|---|---|
| `key` | yes | Regex `^[a-zA-Z0-9 _./&-]+$`, ≤150. Existing key ⇒ update |
| `data_type` | yes | `text` \| `number` \| `date` \| `datetime` \| `dropdown` |
| `values` | dropdown | Array of strings. **Replaces** the whole option list |
| `description` | no (treat as required) | ≤1000. One sentence for the AI: what to extract and when. Undescribed attributes extract badly |
| `new_key_name` | rename | Same charset rules |

```json
{
  "key": "Decision Timeline",
  "data_type": "dropdown",
  "values": ["This week", "This month", "Next 3 months", "Just exploring"],
  "description": "How soon the lead intends to decide; phrases like 'next week' map to This month"
}
```

Response `{ success, message: "Attribute created successfully." | "Attribute updated successfully.", attributes: [...] }`. Changing `data_type` or `values` on an existing attribute triggers a background cleanup; the response may carry `sync_status: "running"`. Free-tier orgs have an attribute cap and get an upgrade payload instead.

### DELETE /custom-attributes

Body: `{ "attributes": "{\"Key A\":true,\"Key B\":true}" }` — a JSON **string** whose keys are attribute keys.

### What to create (build guidance)

6–12 attributes: lead identity (`Name` text, `Age` number, `City` text, `Email`, `Phone` when the flow asks), one dropdown per qualification question with options, the workflow-state attributes named in the qualification spec's *Attribute & State Mapping* (names and values reproduced **exactly**, the qualification prompt reads and writes them), free-text attributes for open questions, and the industry intent field (`Treatment Interested In`, `Course Interested In`, `Property Type`, …). Prefer dropdown whenever the value set is enumerable.

---

## 8. Sequences (auto-responder)

### GET /auto-responder/sequences

Query `sequence_id`, `query`, `include_reminders` optional (`enabled` is accepted but ignored; see §8). Returns `{ data: [ { id, sequence_name, description, mode, whatsapp_account_id, messages: [ { id, message_name, message_type, subject, content, message_delay, order, files } ] } ] }`. Unpaginated. Full bodies are returned, so the update agent summarises the list and fetches one sequence when editing.

### POST /auto-responder/sequence — create or update

| Field | Required | Notes |
|---|---|---|
| `id` | update | `required_without:sequence_name` |
| `sequence_name` | create | `required_without:id`, ≤255, unique per org |
| `description` | no | ≤500 chars (varchar) |
| `mode` | yes | `extension` (WhatsApp via Chrome extension / WAHA) or `whatsapp_api` |
| `whatsapp_account_id` | when `mode=whatsapp_api` | An **active** id from `GET /whatsapp/accounts` |
| `messages[]` | no | Upserted per message: with `id` updates that message, without `id` creates one. Messages not in the payload are **kept**, never deleted (see below) |

Each `messages[]` entry:

| Field | Required | Notes |
|---|---|---|
| `id` | update | Existing message id; omit for new |
| `message_name` | `required_without:id` | Internal label, ≤255. Unique within the sequence |
| `message_type` | yes | `whatsapp` \| `email` \| `reminder` \| `whatsapp_template` \| `ai_call` (see table below) |
| `subject` | `email` | ≤255. Unique within the sequence |
| `content` | all types except `whatsapp_template` | Body. Personalise with `{lead_first_name}` only. Kraya rejects a message whose content closely matches another in the same sequence |
| `message_delay` | yes | Schedule object, see below. `recurring` is not allowed on `ai_call` |
| `order` | yes | 1-indexed |
| `files` | no | Array of `{ url, name }` with absolute URLs (upload first via `POST /upload` or use a public URL) |
| `send_as_caption` | no | boolean, send `content` as the media caption |
| `template_id` | `whatsapp_template` | id of an **APPROVED** template on the org |
| `template_retry_limit` | no | 0–5 retries when the template send fails (default 0) |
| `call_retry_limit` | `ai_call` | 1–10 |
| `call_retry_interval` | `ai_call` | ≥1 |
| `call_retry_interval_unit` | `ai_call` | `minutes` \| `hours` |
| `call_outcome_instructions` | no | ≤5000, how to classify the call outcome |

Message types:

| `message_type` | Channel | What `content` is | Requirements |
|---|---|---|---|
| `whatsapp` | WhatsApp text via extension / WAHA (`mode=extension`) or via the API account (`mode=whatsapp_api`, inside the 24h session window only) | the message body | — |
| `whatsapp_template` | Meta-approved template through the WhatsApp Business API; the only way to message outside the 24h window | ignored | `mode=whatsapp_api`, `whatsapp_account_id` on the sequence, `template_id` of an approved template on that org (`"Only approved templates can be used in sequences."`, `"Template not found or does not belong to your organization."`) |
| `email` | email from the org's from-name/reply-to | HTML/text body | `subject` |
| `reminder` | internal call reminder for the assigned user, nothing is sent to the lead | reminder note | — |
| `ai_call` | outbound AI voice call | instructions injected into the calling agent | `call_retry_*` fields; the org needs an active AI calling agent configured by Kraya (calls are silently skipped otherwise) |

Template variables are filled at send time, not in the sequence: every named `{{var}}` in the template body is resolved from the lead, in this priority: the lead's custom attribute with that exact key, then the built-ins `lead_name`, `lead_first_name`, `name`, `email`, `org_name`, `user_name`, `booked_slot`, then the template's example value. Name template variables after real attribute keys (e.g. `{{Course Interested In}}`) or the built-ins so they resolve.

Template sequence message example (inside `messages[]`, sequence has `"mode": "whatsapp_api", "whatsapp_account_id": 77`):

```json
{
  "message_name": "Day-2 reminder template",
  "message_type": "whatsapp_template",
  "template_id": 4412,
  "template_retry_limit": 2,
  "message_delay": { "schedule": "after_x_units", "after_x_units": { "delay": 2, "unit": "days" } },
  "order": 2
}
```

#### `message_delay` wire format

The agents author `{ value, unit }` and translate it. Kraya only accepts:

```json
{ "schedule": "immediate" }
{ "schedule": "after_x_units",  "after_x_units":  { "delay": 2, "unit": "days" } }   // unit: minutes | hours | days, delay ≥ 1
{ "schedule": "before_x_units", "before_x_units": { "delay": 1, "unit": "hours" } }
{ "schedule": "specific_time",  "specific_time":  { "hours": 10, "minutes": 30, "day_of_week": "monday" } }
{ "schedule": "recurring",      "recurring":      { ... } }
```

No other keys are allowed beside `schedule` and the matching sub-object. Translation used by the agents: `value === 0` → `immediate`; otherwise `after_x_units` with `delay = value`. Delay is relative to the previous message (or sequence start for message 1).

```json
{
  "sequence_name": "Post-Qualified Nurture",
  "description": "5-message nurture for leads who finished qualification but haven't booked",
  "mode": "extension",
  "messages": [
    {
      "message_name": "Welcome + brand intro",
      "message_type": "whatsapp",
      "content": "Hi {lead_first_name}, thank you for showing interest in *Fix My Hair*.\n\nWhat we offer:\n- Hair transplant (FUE / FUT)\n- PRP therapy\n- Scalp treatments\n\nReply *DETAILS* and we'll share the full treatment info.",
      "message_delay": { "schedule": "after_x_units", "after_x_units": { "delay": 5, "unit": "minutes" } },
      "order": 1
    },
    {
      "message_name": "Proof: customer testimonial",
      "message_type": "whatsapp",
      "content": "Real results from people in your shoes.\n\n\"Got my confidence back in three months.\" Rahul, Bangalore.\n\nReply *BOOK* to schedule your consultation.",
      "message_delay": { "schedule": "after_x_units", "after_x_units": { "delay": 2, "unit": "days" } },
      "order": 2
    }
  ]
}
```

Response: `{ success: true, message: "Sequence created successfully" | "Sequence updated successfully", data: { id, sequence_name, ..., messages: [...] } }`. Read the id from **`data.id`**.

Errors: `"Sequence with this name already exists."`, `"Message with name '<x>' already exists in this sequence."`, `"Message with similar content to '<x>' already exists in this sequence."`, `"Message with this subject '<x>' already exists in this sequence."`, `404 "Sequence not found."`.

### How the update treats `messages[]` and `enabled`

Verified against `AutoResponderController@upsertSequence` and `RuleService` on Kraya-Laravel `main`, 2026-09-15.

**Messages are upserted one at a time, never replaced.** An entry with an existing `id` updates that message; an entry without `id` creates a new one. Messages already on the sequence that are not in the payload are left untouched: this endpoint never deletes a message. In practice:
- To edit one message, send the sequence `id`, `mode`, and just that message with its `id`. The other messages stay as they are.
- To remove messages, call `DELETE /auto-responder/sequence-message` once per message id. To restructure, delete the messages you are dropping, then send the ones you keep with their ids and the new ones without.
- `order` is not renumbered for you. After adding or deleting, send every remaining message's `id` with its intended `order` so positions stay unique and sequential.
- Re-sending an existing message without its `id` does not overwrite it. It is validated as a new message, fails with `409 "Message with name '<x>' already exists in this sequence."` or `409 "Message with similar content to '<x>' already exists in this sequence."`, and the whole update (sequence and every message in the payload) is rolled back.
- After any change, read it back with `GET /auto-responder/sequences?sequence_id=<id>` and compare the message list with what you intended.

**`enabled` is not a switch; ignore it.** This endpoint does not write `enabled` (it saves `sequence_name`, `description`, `mode`, `whatsapp_account_id`, `organization_id` and a generated `sequence_key`, never `enabled`), no route sets it, and nothing that sends messages reads it, in Laravel, the Chrome extension or the dashboard. Rules start a sequence through `activeSequence()`, which excludes only deleted sequences, so a sequence showing `enabled: 0` is assigned and sends normally. What actually stops follow-ups: the sequence being deleted, the rules that start it being removed or disabled (`rules.enabled` is a real gate), a `stop_assigned_sequence` rule, or `auto_responder_enabled` switched off on the WhatsApp API account (§13) or the user (§20). The `enabled` query parameter on `GET /auto-responder/sequences` is accepted and has no effect (the extension sends `enabled=1` and receives every non-deleted sequence).

### DELETE /auto-responder/sequence — body `{ "id": 4812 }`
### DELETE /auto-responder/sequence-message — body `{ "id": 19201 }`

Both take the id in the JSON body, not the path.

#### Content conventions the agents enforce

Short paragraphs separated by blank lines; `*bold*` WhatsApp markdown; `-` bullets; no em/en dashes (normalise to commas); emoji only when the client asked for them (default none, see the skill's content rules); one `Reply *KEYWORD* to <payoff>` CTA per message; `Reply *STOP* to unsubscribe.` last line on marketing nurture; `{lead_first_name}` only, never `{lead_name}`; never ship ops notes, `[Insert …]`, `TBD` or a placeholder testimonial. 4–6 messages per sequence.

---

## 9. Organization info (the AI's memory)

### Reading it

`GET /users/metadata` → `user.organization.info`. There is no dedicated GET.

### POST /organizations/info — full replace

| Field | Required | Notes |
|---|---|---|
| `org_name` | **yes, every call** | |
| `about` | **yes, every call** | Everything the AI must always know: summary, services, audience, locations, pricing model, website, phone, email, policies. Blanked if omitted |
| `qualification_requirements` | no (replaces) | The whole qualification block as one markdown string, see below |
| `attachments` | no (replaces) | **JSON-encoded string** of `[{ url, name }]`. Each URL is scraped into the knowledge base (Pinecone). Always include the client website; add brochure / price-list PDFs by uploading them first (§14) and passing the returned URL |
| `bot_languages` | send always | Comma-separated, ≤500, e.g. `"English, Hindi"`. Absent ⇒ blanked. Default `"English"` |
| `sendable_files` | no (replaces) | JSON string of `[{ id (uuid you generate), url (from §14), name, type: document\|image\|video, description (1–500, written for the AI: what the file is and when to send it), created_at? }]`. These are files the AI can *send* to a lead mid-chat (brochure, price list, sample report). Per-pack count limit; over the limit returns `400 "Sendable files limit exceeded…"` |

```json
{
  "org_name": "Fix My Hair",
  "about": "Fix My Hair is a Bangalore-based clinic ...\n\nWebsite: https://fixmyhair.example\nContact: +91-9876543210\n\nPolicies the AI must follow:\n- Never discuss medical outcomes; defer to the doctor.",
  "bot_languages": "English, Hindi",
  "attachments": "[{\"url\":\"https://fixmyhair.example\",\"name\":\"Fix My Hair Website\"}]",
  "qualification_requirements": "## Rules\n1. ...\n\n## Welcome Message\n<welcome_message>\n...\n</welcome_message>\n\n## Qualification Questions\n\n### Question 1\n<question_content>\n...\n</question_content>\n\n## Attribute & State Mapping\n...\n\n## Stage Shifting Logic\n...\n\n## Edge Cases\n...\n\n## Final Acknowledgment Message\n<acknowledgement_message>\n...\n</acknowledgement_message>\n\n## Qualification\nThe lead is qualified after Q1 and Q2 are answered."
}
```

Response `{ message: "Organization Information Updated.", success: true }`. Saving re-indexes `about` + qualification into the vector store asynchronously.

#### `qualification_requirements` structure

Up to eight `##` sections, all carried through verbatim when present: `Rules` (numbered), `Welcome Message` (`<welcome_message>` tags), `Qualification Questions` (`### Question N` + `<question_content>` tags, options as `-` bullets one per line), `Attribute & State Mapping` (attribute names and values must match the custom attributes exactly), `Stage Shifting Logic` (stage names must match real stages exactly), `Edge Cases`, `Final Acknowledgment Message` (`<acknowledgement_message>` tags), `Qualification` (qualified-when / do-not-qualify-when). Messages inside the tags follow the same formatting rules as sequences.

---

## 10. FAQs

### GET /faqs/categories — query `page`, `count` (default 10), `id`, `include_empty`

Returns `{ data: [ { id, title, description, article_count } ] }`.

### GET /faqs/articles — query `page`, `count` (default 10), `category_id`, `id`

Returns `{ data: [ { id, title, content, category: { id, title }, updated_by, last_updated, created_at } ] }`.

### POST /faqs/articles — create or update

| Field | Required | Notes |
|---|---|---|
| `id` | update | |
| `category_id` | `required_without:category_name` | |
| `category_name` | `required_without:category_id` | find-or-create by title |
| `category_description` | with `category_name` | |
| `title` | yes | The question |
| `content` | yes | The answer, 2–4 specific sentences |
| `attachments` | no | array |

Response: `{ id, category: { id, title, description }, title, content, attachments, updated_by, last_updated }`. Each save vector-indexes the article.

### DELETE /faqs/articles — body `{ "id": 3410 }` (one per call)
### PUT /faqs/categories/{id} — body `{ "title": "...", "description": "..." }` (`title` required, ≤255; note **title**, not name)
### DELETE /faqs/categories/{id} — deletes the category and its articles

Build guidance: 15–30 FAQs grounded in the client's own materials, categories only where supported (Pricing, Process, Eligibility, Logistics, Trust, Services, Objections). When the source lacks the answer, write a hand-off ("Our team will share specifics — please share your contact details") rather than inventing numbers.

---

## 11. Rules (smart triggers)

### GET /rules — query `search` optional

Returns `{ success, data: [ { id, name, description, trigger_type, action_type, enabled, processing_order, trigger_conditions: [...], attribute_conditions: [...], keyword_trigger_keywords, no_response_duration_value, ..., action_stage, action_pipeline, action_sequence, action_rr_pipelines } ] }`. Full objects; unpaginated.

### POST /rules — create or update

Core fields:

| Field | Required | Notes |
|---|---|---|
| `rule_id` | update | **not `id`** |
| `name` | yes | ≤255 |
| `description` | yes | |
| `trigger_type` | yes | see table |
| `action_type` | yes | see table |
| `enabled` | no | default true |
| `trigger_conditions[]` | yes | `min:1` for every trigger. Entries are OR'd. Each `{ condition_stage_id, condition_pipeline_id, condition_sequence_id? }`. **Replaces** on update |
| `attribute_conditions[]` | no | `{ attribute_key, match_type: equals\|contains, match_values: [...] }`. AND'd together. **Replaces** on update |

Trigger types and their extra fields:

| `trigger_type` | Fires when | Extra required fields |
|---|---|---|
| `lead_moved_to_stage` | lead enters a listed stage | `condition_stage_id` + `condition_pipeline_id` per condition |
| `new_lead_created` | lead created in stage/pipeline | same |
| `no_response_from_lead` | silence for N | same + `no_response_duration_value` (≥1; ≥5 if minutes), `no_response_duration_unit`: `minutes` \| `hours` |
| `keyword_detected` | inbound message matches | same + `keyword_trigger_keywords[]` (1–50; `"*"` = any message) |
| `days_in_stage` | lead sat in stage for N | same + `days_in_stage_value`, `days_in_stage_unit`: `hours` (≤17520) \| `days` (≤730) |
| `call_logged` | call logged with status | same + `trigger_call_status`: `done` \| `no_response` \| `missed` |
| `sequence_completed` | assigned sequence finished | **all three**: `condition_stage_id`, `condition_pipeline_id`, `condition_sequence_id`. Repeat the sequence id across one condition per stage where the lead might be |

Action types and their extra fields. Send **only** the fields for the chosen action: Kraya rejects stray ones (`"Set Call Reminder action does not require action_stage_id"`, `"Round Robin Assignment action does not require action_sequence_id"`, `"Schedule a Message action does not require action_pipeline_id"`, …).

| `action_type` | Extra required fields |
|---|---|
| `move_to_stage` | `action_stage_id`, `action_pipeline_id` (stage must belong to that pipeline) |
| `initiate_sequence` | `action_sequence_id`; optional `action_overwrite_sequence` (bool, replace any running sequence) |
| `stop_assigned_sequence` | — |
| `set_call_reminder` | `action_call_reminder_value` (≥1; ≤730 days / ≤17520 hours / ≤1051200 minutes), `action_call_reminder_unit`: `minutes`\|`hours`\|`days`; optional `action_call_reminder_note` (≤2000), `action_call_reminder_overwrite` (bool) |
| `toggle_ai` / `toggle_auto_followup` | `action_value`: `on` \| `off` |
| `round_robin_assignment` | `action_rr_pipeline_ids[]` (≥1). Every pipeline must be a Sales pipeline with round robin enabled and belong to the org. Not allowed with `no_response_from_lead`. No stage/pipeline/sequence action fields |
| `send_email` | `action_email_subject` (≤200), `action_email_body` (≤100000), `action_email_send_to`: `lead` (the lead's own email) \| `custom`; when `custom`, `action_email_to[]` (≤10, distinct, valid emails) |
| `send_template_message` | `action_whatsapp_account_id` (org's own account), `action_template_id` — must be **APPROVED** and belong to that account (`"The selected template does not belong to the WhatsApp account or is not approved"`), and every body/header variable needs an example value on the template. Sends immediately when the rule fires; variables resolve as in §8 |
| `set_lead_attribute` | `action_attribute_key_id` (numeric attribute **id** from `GET /custom-attributes`, not the key), `action_attribute_value` — validated against the attribute's `data_type`: text ≤10000, number numeric, date `DD-MM-YYYY`, datetime `DD-MM-YYYY HH:mm:ss`, dropdown must be one of `values` |
| `schedule_message` | `action_message_type`: `text` \| `template`; for `text`: `action_message_body` (≤4096, sent through the extension/WAHA session); for `template`: `action_whatsapp_account_id` + `action_template_id` (same approval rules as above) plus optional `action_message_max_retries` ∈ {0,1,2,3,5} and `action_message_retry_after_hours` ∈ {8,12,16,24,48} (required when retries > 0). Plus `action_schedule_time_type` (below) |

`schedule_message` timing (`action_schedule_time_type`):

| Value | Extra fields | Meaning |
|---|---|---|
| `fixed_time` | `action_schedule_fixed_time` `HH:MM` | next occurrence of that clock time in the org timezone |
| `relative` | `action_schedule_relative_value` (≥1; same caps as call reminders), `action_schedule_relative_unit`: `minutes`\|`hours`\|`days` | offset from when the rule fires |
| `from_attribute` | `action_schedule_attribute_key_id` | exact datetime read from a lead attribute; the attribute **must** be `data_type = datetime` (`"The selected schedule attribute must be a datetime attribute"`) |

Examples:

```json
// Template on keyword — needs WhatsApp API
{
  "name": "Send brochure template on BROCHURE",
  "description": "When the lead replies BROCHURE, send the approved brochure template",
  "trigger_type": "keyword_detected",
  "trigger_conditions": [ { "condition_stage_id": 5502, "condition_pipeline_id": 901 } ],
  "keyword_trigger_keywords": ["brochure", "catalog"],
  "action_type": "send_template_message",
  "action_whatsapp_account_id": 77,
  "action_template_id": 4412,
  "enabled": true
}

// Set a workflow-state attribute when a lead lands in a stage
{
  "name": "Mark callback requested",
  "description": "Set Callback Requested = Yes when the lead enters Human Intervention",
  "trigger_type": "lead_moved_to_stage",
  "trigger_conditions": [ { "condition_stage_id": 5510, "condition_pipeline_id": 901 } ],
  "action_type": "set_lead_attribute",
  "action_attribute_key_id": 318,
  "action_attribute_value": "Yes"
}

// Appointment reminder 2 hours before the booked datetime attribute
{
  "name": "Visit reminder from Appointment Time",
  "description": "Schedule the visit-reminder template at the lead's Appointment Time",
  "trigger_type": "lead_moved_to_stage",
  "trigger_conditions": [ { "condition_stage_id": 5511, "condition_pipeline_id": 901 } ],
  "action_type": "schedule_message",
  "action_message_type": "template",
  "action_whatsapp_account_id": 77,
  "action_template_id": 4420,
  "action_schedule_time_type": "from_attribute",
  "action_schedule_attribute_key_id": 322,
  "action_message_max_retries": 2,
  "action_message_retry_after_hours": 8
}

// Plain-text nudge 3 days after qualification, via the extension session
{
  "name": "3-day text nudge",
  "description": "Schedule a short check-in text 3 days after the lead qualifies",
  "trigger_type": "lead_moved_to_stage",
  "trigger_conditions": [ { "condition_stage_id": 5502, "condition_pipeline_id": 901 } ],
  "action_type": "schedule_message",
  "action_message_type": "text",
  "action_message_body": "Hi {lead_first_name}, still keen on the consultation? Reply *BOOK* and we'll lock a slot.",
  "action_schedule_time_type": "relative",
  "action_schedule_relative_value": 3,
  "action_schedule_relative_unit": "days"
}
```

```json
{
  "name": "DNP re-engagement at 24h silence",
  "description": "If no response for 24h while qualified, start the re-engagement sequence",
  "trigger_type": "no_response_from_lead",
  "trigger_conditions": [
    { "condition_stage_id": 5502, "condition_pipeline_id": 901 },
    { "condition_stage_id": 5501, "condition_pipeline_id": 901 }
  ],
  "no_response_duration_value": 24,
  "no_response_duration_unit": "hours",
  "action_type": "initiate_sequence",
  "action_sequence_id": 4813,
  "action_overwrite_sequence": false,
  "enabled": true
}
```

Response `{ success: true, data: { id, ... } }`. Read the id from **`data.id`**.

Errors: `"The specified stage does not belong to the specified pipeline"`, `"The specified sequence does not belong to your organization"`, `"Round Robin Assignment is not allowed with \"No response from lead\" trigger"`, and the identical-rule rejection in §3.

### DELETE /rules/{id} → `{ success, message: "Rule deleted successfully" }`
### POST /rules/reorder — body `{ "rule_orders": [ { "rule_id": 721, "processing_order": 0 }, ... ] }`

All matching rules fire, in `processing_order`.

#### Core rule set for a new account

Build these deterministically from stored ids; drop any extracted rule that duplicates one:

1. Stop on STOP — `keyword_detected` (`stop`, `unsubscribe`, `not interested`, `dont message`, `don't message`) on all stages → `stop_assigned_sequence`; a second rule on the same keywords → `toggle_ai` off.
2. Stop on Win / Lost — `lead_moved_to_stage` into `Lead Won` / `Lead Lost` → `stop_assigned_sequence`.
3. Silence routing — `no_response_from_lead` (24h/48h/72h by cycle) in `New Lead` + `Qualified` → `move_to_stage` `No Response`.
4. Re-engagement — `lead_moved_to_stage` into `No Response` → `initiate_sequence` (DNP sequence).
5. Chain end — `sequence_completed` (DNP) → `move_to_stage` (dormant/cold stage).
6. Handoff — `lead_moved_to_stage` into `Human Intervention` → `toggle_ai` off, plus `stop_assigned_sequence`.
7. Stage → its sequence for every (stage, sequence) pair that exists.

Consolidate: one rule with many `trigger_conditions` beats N rules, across stages **and** pipelines. Never create intent-keyword → Qualified rules (qualification is the AI's job).

When the org has a WhatsApp API account, the same set applies with these upgrades: silence routing and re-engagement can use `send_template_message` / `schedule_message` templates (they work outside the 24h window, plain text does not), and booking-style flows can schedule a reminder template `from_attribute` off a datetime attribute such as `Appointment Time`.

---

## 12. Quick replies

Canned slash-command messages for human agents. Kraya seeds 4 defaults at signup.

### GET /quick-replies — query `category`, `key` optional

Returns a nested object, **not** an array:

```json
{
  "Initial Response": {
    "_group_id": 12, "_group_order": 0,
    "call-not-picked": { "id": 28106, "name": "Call Not Picked", "key": "call-not-picked", "category": "Initial Response", "content": "...", "order": 0, "attachments": [] }
  },
  "Objections": { "_group_id": 13, "_group_order": 1, "fee-is-high": { ... } }
}
```

Skip keys starting with `_` when iterating replies.

### POST /quick-replies — create or update

| Field | Required | Notes |
|---|---|---|
| `id` | update | |
| `name` | yes | Operator-facing situation label (`Call Not Picked`, `Fee is high`) |
| `key` | yes | Slash command, unique per org (`/pricing`). Builder uses the slugified name, ≤100 |
| `category` | yes | Group **name**; Kraya find-or-creates the group |
| `content` | yes | May use `{lead_name}`, `{user_name}`, `{org_name}` |
| `attachments` | no | JSON **string** of `[{ url, name }]` |

Response: the quick reply object. Duplicate name/key ⇒ treat "already exists" as skip.

### DELETE /quick-replies/{id} → `{ message: "Quick reply deleted" }`
### POST /quick-replies/groups/reorder — `{ "group_orders": [ { "group_id": 12, "order": 0 }, ... ] }`
### POST /quick-replies/reorder — `{ "quick_reply_orders": [ { "quick_reply_id": 28106, "order": 0 }, ... ] }`

Build guidance: 15–20 replies across `Initial Response`, `Info & Links`, `Follow-Up`, `Objections`, `Payment & Next Step`, `Closing & Reactivation`; 150–450 chars each; every reply carries a real specific from the client's materials.

---

## 13. WhatsApp API accounts and templates

Only relevant when the org has a Meta WhatsApp Business API account connected. Templates cannot exist without one.

### GET /whatsapp/accounts

Returns `{ data: [ { id, name, display_phone_number, phone_number, phone_number_id, status, ai_replies_enabled, auto_create_leads, bump_up_enabled, max_bump_up, auto_responder_enabled, auto_responder_hours, auto_responder_auto_pause, auto_responder_pause_delay, new_lead_broadcast_enabled, new_lead_broadcast_template_id, new_lead_broadcast_template, request_contact_info_template_id, request_contact_info_template, affected_sequences_count, mm_lite_active, coexistence_enabled } ] }`. Usable only when `status === "active"`. Empty list ⇒ no WABA; tell the rep templates are unavailable. `GET /users/metadata` → `has_whatsapp_business_account` is a cheap pre-check.

### POST /whatsapp/accounts — settings of an already connected account

Connecting the account is out of scope; this configures one that ops has already connected. All fields are `sometimes`, so send only what you change.

| Field | Notes |
|---|---|
| `whatsapp_account_id` | **required**, from `GET /whatsapp/accounts` |
| `ai_replies_enabled` | AI qualification replies on this number |
| `auto_create_leads` | create a lead for every new inbound number (the validator also accepts the singular `auto_create_lead`) |
| `bump_up_enabled`, `max_bump_up` | nudge silent leads after the AI replies; count of nudges |
| `auto_responder_enabled` | auto-followup sequences run on this number |
| `auto_responder_hours` | JSON **string** `{"start":"09:00","end":"20:00"}` (24h `HH:MM`) — sequences send only inside this window |
| `auto_responder_auto_pause`, `auto_responder_pause_delay` | pause the sequence when the lead replies; delay is a JSON **string** `{"value":2,"unit":"hours"}` with `unit` in `minutes`\|`hours`\|`days` |
| `new_lead_broadcast_enabled`, `new_lead_broadcast_template_id` | send an approved template to every new lead automatically. Template must be `APPROVED` on this account; `null` clears it |
| `request_contact_info_template_id` | approved template the AI uses to ask a lead for contact details; `null` clears |

```json
{
  "whatsapp_account_id": 77,
  "ai_replies_enabled": true,
  "auto_create_leads": true,
  "auto_responder_enabled": true,
  "auto_responder_hours": "{\"start\":\"09:00\",\"end\":\"20:00\"}",
  "auto_responder_auto_pause": true,
  "auto_responder_pause_delay": "{\"value\":2,\"unit\":\"hours\"}",
  "new_lead_broadcast_enabled": true,
  "new_lead_broadcast_template_id": 4412
}
```

Response `{ success: true, data: { id, name, display_phone_number, ai_replies_enabled, status, auto_create_leads, bump_up_enabled, max_bump_up, auto_responder_enabled, auto_responder_hours, auto_responder_auto_pause, new_lead_broadcast_enabled, new_lead_broadcast_template_id, ... } }`. `400 "Invalid template. Template must be approved and belong to this WhatsApp account."` when a template id is wrong.

### GET /whatsapp/templates

Query: `template_id`, `status[]` (`DRAFT`|`PENDING`|`APPROVED`|`REJECTED`), `categories[]` (`MARKETING`|`UTILITY`|`AUTHENTICATION`), `whatsapp_account_id[]`, `query`, `page`, `count` (**default 10 — always pass `count=1000`**), `sort_by`, `sort_order`. Arrays are Laravel-style `status[]=APPROVED&status[]=PENDING`.

Returns `{ success, data: [ { id, name, display_name, status, category, rejection_reason, whatsapp_account_id (string!), components: [...] } ] }`. Compare `whatsapp_account_id` with `Number()`.

### POST /whatsapp/template — create (and submit)

| Field | Required | Notes |
|---|---|---|
| `whatsapp_account_id` | yes | Active account id, never guessed |
| `display_name` | yes | ≤512. Kraya does not reject duplicates |
| `category` | yes | `MARKETING` \| `UTILITY` (carousel must be MARKETING) |
| `status` | yes | Send `"DRAFT"` |
| `submit` | no | `true` (default in the agent) submits to Meta ⇒ status `PENDING`; `false` saves a draft |
| `components[]` | yes | see below |
| `id` | update | |

`components` (types UPPERCASE; Kraya transforms for Meta):

```json
[
  { "type": "HEADER", "format": "TEXT", "text": "Hello {{1}}", "example": { "header_text": ["Rahul"] } },
  // or media header: { "type": "HEADER", "format": "IMAGE"|"VIDEO"|"DOCUMENT", "example": { "header_handle": ["https://public.url/file.jpg"] }, "attachment_file_name": "brochure.pdf" }
  { "type": "BODY", "text": "Hi {{first_name}}, your consultation is on {{date}}.", "example": { "body_text": [["Rahul", "12 Sep"]] } },
  { "type": "FOOTER", "text": "Reply STOP to opt out" },
  { "type": "BUTTONS", "buttons": [
      { "type": "QUICK_REPLY", "text": "Book now" },
      { "type": "URL", "text": "View", "url": "https://x.example/{{1}}", "example": ["https://x.example/abc"] },
      { "type": "PHONE_NUMBER", "text": "Call us", "phone_number": "+919876543210" },
      { "type": "COPY_CODE", "example": ["SAVE20"] }
  ] }
]
```

Rules: body variables use **named** placeholders (`{{first_name}}`), Kraya numbers them; a variable may not be the very first or last token of the body (`400 "Variables cannot be placed at the beginning or end of the message body."`); one example value per variable in first-appearance order; button text ≤25, header/footer text ≤60. Carousel: `{ "type": "CAROUSEL", "cards": [ { "components": [ media HEADER, BODY, BUTTONS(≤2) ] }, ... ] }` with 2–10 cards, every card using the same button types in the same order, no top-level header/footer.

Response `201 { success: true, message: "Template created successfully", data: { id, status: "PENDING", ... } }`. Approval is asynchronous: Meta reviews, Kraya's webhook flips the status to `APPROVED` or `REJECTED` (with `rejection_reason`). Check with `GET /whatsapp/templates?template_id=<id>`.

Only `APPROVED` templates can be referenced by `whatsapp_template` sequence messages or by `send_template_message` / `schedule_message` rules, so when a build needs templates, create and submit them first, then wait for approval before wiring sequences and rules to them. Every variable must carry an example value or the template is rejected at wiring time.

`422 "The specified WhatsApp account does not belong to your organization or is not active."` when the id is wrong.

### DELETE /whatsapp/template/{id}

Soft-deletes the template and detaches it from sequences; sequences left with zero messages are soft-deleted too.

---

## 14. File upload

### POST /upload — multipart

| Field | Required | Notes |
|---|---|---|
| `file` | yes | Any file. Size is bounded by the server upload limits; keep client files small (a few MB) |
| `folder` | no | Only `faqs` is accepted; use it for FAQ attachments, omit otherwise |

```bash
curl -X POST "$BASE/upload" -H "Authorization: Bearer $TOKEN" -F "file=@./brochure.pdf"
```

Response `{ "url": "https://kraya-files.s3.ap-south-1.amazonaws.com/<org-id>/<hash>.pdf" }`. The URL is public and scoped to the org's folder.

Use the URL in: org info `attachments` (knowledge base, PDFs are scraped), org info `sendable_files` (files the AI can send), sequence message `files[]`, quick reply `attachments`, FAQ article `attachments`, and WhatsApp template media headers (`example.header_handle`). Public URLs the client already hosts work everywhere too; upload only when the file is not online.

---

## 15. Bump-ups

**Availability.** The customer-facing endpoint below is on the `staging` branch and deployed to `api-staging.kraya-ai.com`. On production (`api.kraya-ai.com`) it does not exist yet and returns `404`; only Kraya's internal SuperAdmin can change bump-ups there. If you get a 404, do not probe other paths: note in the handover that bump-ups still run Kraya's seeded steps (step 1 contains an emoji) and ask ops to adjust them from the dashboard once the release ships.

Bump-ups are the short nudges Kraya sends when a lead goes silent after an AI reply. Two modes: **AI** (`fixed_bump_ups: false`, the LLM writes each nudge from context) or **Fixed** (`fixed_bump_ups: true`, ops-authored steps). The per-user / per-account switches `bump_up_enabled` and `max_bump_up` (max 5) decide whether and how many fire; this endpoint decides what they say.

### Read

`GET /users/metadata` → `user.organization.fixed_bump_ups`, `bump_up_variation_enabled`, `bump_up_steps[]` (works on production too).

### POST /organizations/bumpups

| Field | Required | Notes |
|---|---|---|
| `fixed_bump_ups` | yes | `true` = use the steps below; `false` = AI-written nudges (stored steps are kept for switching back) |
| `bump_up_variation_enabled` | no | when `true`, the LLM rewords each fixed step per lead so repeated nudges do not look identical |
| `bumpups[]` | with fixed mode | 1–5 steps; omit to flip the mode without touching stored steps. Switching to fixed on an org with no steps seeds a default schedule |
| `bumpups[].sort_index` | yes | 0–4, order of the nudge |
| `bumpups[].message` | yes | ≤4000, `{lead_first_name}` allowed; same copy rules as sequences |
| `bumpups[].delay_minutes` | yes | 1–10080 (7 days) after the previous message |

```json
{
  "fixed_bump_ups": true,
  "bump_up_variation_enabled": true,
  "bumpups": [
    { "sort_index": 0, "message": "Hi {lead_first_name}, did you get a chance to see the details above? Happy to answer anything.", "delay_minutes": 120 },
    { "sort_index": 1, "message": "Just checking whether the consultation slot still works for you. Reply *BOOK* and we'll lock it in.", "delay_minutes": 1440 }
  ]
}
```

Response `{ success: true, message: "Bump-up configuration updated successfully.", organization: { fixed_bump_ups, bump_up_variation_enabled, bump_up_steps: [...] } }`. Validation errors come back as `{ success:false, message: "Validation failed", errors }` with labels like `"Bumpup 2 delay cannot be more than 7 days (10080 minutes)."`.

---

## 16. Calendar booking (Basic / Pro packs)

A public booking page at `https://<app>/book-a-call/<org_slug>` (also `/calendar/<calendar_slug>`) that creates or updates a lead, sets `lead.booked_at`, and can run a reminder sequence anchored to that slot. Every call returns `403 "Calendar is available on Basic and Pro plans"` on a free org.

### GET /calendar/settings

Returns `{ success, data: { id, enabled, calendar_slug, logo_url, title, sub_heading, description, key_highlights[], session_title, calendar_content, discussion_points[], show_session_details, confirmation_heading, confirmation_content, show_booking_details, show_add_to_calendar, send_calendar_invite_to_lead, redirect_enabled, redirect_cta_text, redirect_url, form_fields, available_days, working_hours_start, working_hours_end, max_days_open, minimum_notice_minutes, slot_interval_minutes, slots_per_interval, max_slots_per_day, blackout_time_ranges[], block_specific_dates[], block_weekdays[], reminder_sequence_id } }`.

### PUT /calendar/settings

All fields `sometimes`; send only what you change.

| Group | Fields |
|---|---|
| Switch | `enabled` (bool), `calendar_slug` (`[a-z0-9-]`, ≤255) |
| Page copy | `title`, `sub_heading` (≤255), `description`, `key_highlights[]` (≤6 strings), `session_title`, `calendar_content` (session description), `discussion_points[]`, `show_session_details`, `logo_url` |
| Confirmation | `confirmation_heading`, `confirmation_content`, `show_booking_details`, `show_add_to_calendar`, `send_calendar_invite_to_lead`, `redirect_enabled`, `redirect_cta_text`, `redirect_url` |
| Availability | `available_days` — either `["monday","tuesday",…]` (uniform hours) or `[{ "day": "monday", "from": "09:00", "to": "17:00" }, …]`; `working_hours_start` / `working_hours_end` (`H:MM`); `max_days_open` (1–365); `minimum_notice_minutes` (≥0); `slot_interval_minutes` (5–480); `slots_per_interval` (1–50); `max_slots_per_day` (1–500); `blackout_time_ranges[]` of `{start,end}`; `block_specific_dates[]` (`YYYY-MM-DD`); `block_weekdays[]` |
| Lead form | `form_fields` — object `{ "fields": [...], "settings": {...} }`, see below |

`form_fields.fields[]` entries bind a question on the booking form to a custom attribute, so answers land on the lead:

```json
{
  "fields": [
    { "label": "Which treatment are you interested in?", "attribute_key": "Treatment Interested In", "type": "select", "required": true, "order": 1, "values": ["Hair Transplant", "PRP therapy"] },
    { "label": "City", "attribute_key": "City", "type": "small_text", "required": false, "order": 2, "placeholder": "Bangalore" }
  ],
  "settings": { "accent_color": "#236967", "submit_button_text": "Book my slot", "consent_required": true, "consent_text": "", "form_submission_email": "" }
}
```

`type` follows the attribute's data type: `small_text`, `number`, `date`, `datetime`, `select` (dropdown, carry the attribute's `values`). Name, phone and email are built in and not part of `fields`.

Other calls: `POST /calendar/settings/logo` (multipart `file`, png/jpg/jpeg/svg ≤2 MB, sets `logo_url`), `POST /calendar/settings/reset` (back to defaults).

### Reminder sequence

`POST /calendar/settings/reminder-sequence` with `{ sequence_name, description?, mode: "extension"|"whatsapp_api", whatsapp_account_id? }` creates an empty sequence of type *reminder* and links it to the calendar (`409 "A reminder sequence already exists for this calendar."` if one is linked). Add its messages with the normal `POST /auto-responder/sequence` using the returned `sequence.id`. Delays on reminder messages are anchored to the lead's **booked slot**, not the previous message:

| `message_delay.schedule` | Meaning for a reminder message |
|---|---|
| `before_x_units` `{ delay, unit }` | send N minutes/hours/days **before** the slot (e.g. 24h reminder, 1h reminder) |
| `after_x_units` `{ delay, unit }` | send N after the slot (feedback, no-show follow-up) |
| `specific_time` `{ hours, minutes }` | at that clock time on the slot's date |
| `immediate` | right after booking (confirmation) |

`DELETE /calendar/settings/reminder-sequence` detaches and soft-deletes it. The `{{booked_slot}}` template variable renders the slot in the org timezone for template messages.

---

## 17. Co-Pilot configuration

Co-Pilot flags leads that need human attention (hot intent, stalled follow-ups, overdue calls) and shows them on the dashboard. Per-org settings live in `organization_config`.

### GET /organization-config

Returns `{ config: { copilot_enabled, copilot_call_tracking_start_at, copilot_activation_at, copilot_intent_threshold, copilot_followup_threshold, copilot_exempt_stages, copilot_hot_lead_stages, copilot_thresholds } }`. Only the five keys below are writable; the timestamps and `copilot_thresholds` are Kraya-managed.

### PATCH /organization-config

| Field | Notes |
|---|---|
| `copilot_enabled` | master switch; when `false` nothing is computed for the org |
| `copilot_intent_threshold` | 1–10; intent score at or above which a lead is flagged as hot (default 7) |
| `copilot_followup_threshold` | 1–50; minimum follow-up count before the "too many follow-ups, no reply" flag (default 3) |
| `copilot_exempt_stages` | `[{ pipeline_id, stage_id }]`; leads in these stages are never flagged (won, lost, deleted, parked) |
| `copilot_hot_lead_stages` | `[{ pipeline_id, stage_id }]`; when non-empty, the hot-intent flag also requires the lead to sit in one of these |

```json
{
  "copilot_enabled": true,
  "copilot_intent_threshold": 7,
  "copilot_followup_threshold": 3,
  "copilot_exempt_stages": [ { "pipeline_id": 901, "stage_id": 5505 }, { "pipeline_id": 901, "stage_id": 5506 } ],
  "copilot_hot_lead_stages": [ { "pipeline_id": 901, "stage_id": 5502 }, { "pipeline_id": 901, "stage_id": 5510 } ]
}
```

Unknown keys are silently dropped. Changes take effect on the next compute tick (every 30 minutes). `GET /copilot/flags` lists the current flags per lead (read-only, useful to confirm the config is live).

---

## 18. Testing the AI (public demo chat endpoints)

Kraya exposes the same qualification engine the WhatsApp bot uses through a public demo chat, so the agent can test what it configured without a WhatsApp number or a lead. No auth header, no credits consumed, nothing persisted. Rate limit: 60 calls per minute per IP. The org is addressed by its `slug` (`user.organization.slug` from `/users/metadata`). The same experience is available to humans at the dashboard's `/demo/<org-slug>/ai-chat` page.

### POST /demo/ai-chat/generate-reply

| Field | Notes |
|---|---|
| `org_slug` | required |
| `chat[]` | required, ≥1. Each `{ "from": "other" \| "user", "message": "...", "timestamp"?: unix }`. **`other` is the lead, `user` is the bot.** Send the whole conversation so far, oldest first |

```json
{
  "org_slug": "fix-my-hair",
  "chat": [
    { "from": "other", "message": "Hi, I saw your ad about hair transplant" }
  ]
}
```

Response `{ success, reply, should_send, send_files: [ { file_id, caption, file_url, file_name, file_type } ], change_stage: { stage_id, stage_name } | null }`. The reply is generated with the org's live prompt config (template and model), `about`, `qualification_requirements`, `bot_languages`, `sendable_files`, the pipeline's stage list with descriptions, and the knowledge base, as if the lead were in New Lead. Append the reply as `{ "from": "user", ... }` and the next lead message as `{ "from": "other", ... }` to continue. Custom-attribute extraction is not exercised here; it runs on real leads only.

### POST /demo/ai-chat/check-qualification

Same body. Returns `{ success, qualified: bool, reason }`. `403 "Demo qualification check is not enabled for this organization."` when the org's prompt config has `run_qualification_check` off (read it from `GET /demo/ai-chat/organization-info?org_slug=…` → `organization.run_qualification_check`).

### POST /demo/ai-chat/generate-bumpup

Body `{ org_slug, chat[], index? }`. Returns the next bump-up text the org would send (fixed step `index` when the org is in fixed mode, otherwise an AI-written nudge). `"Max bump up message generation reached!"` past the last step.

### What to test after a build

Run at least three short conversations and read the results against the spec you wrote: (1) a cooperative lead who answers every question in order, expecting the acknowledgement message and `change_stage` to Qualified on the last answer; (2) a lead who opens with a price question or an off-topic question, expecting an answer-then-resume with no invented price; (3) a lead in the client's second language or one who refuses a question, expecting language mirroring per `bot_languages` and no re-ask loop; (4) the conflict-audit probes: one direct question per item the configuration promises (brochure, RERA, address, batch date, payment link) and per fact that had conflicting sources, where a pass is the exact value from org context or a hand-off and anything else is a fail (`conflict-audit.md` §4). Check formatting on every reply (paragraphs, bold, one question per message, no emoji unless the client wants them). Report the transcripts in the handover.

## 19. Leads and conversations (read-only lookups for debugging)

These exist so the agent can turn "the lead who messaged us yesterday about pricing" into a lead id, and then read what was actually said, before it touches configuration. All of them are reads; the agent never creates, edits, or messages leads.

### GET /leads — find a lead

| Query | Notes |
|---|---|
| `page`, `count` | **both required** (start with `page=1&count=20`) |
| `phone` | exact match on the stored phone (digits with country code, no `+`, e.g. `919876543210`). Fastest and most reliable |
| `search` | free text over name, email and phone |
| `id` | one lead by id |
| `chat_id` | the WhatsApp JID stored on the lead (`<digits>@c.us`, or `<id>@lid` for privacy-masked numbers) |
| `stage_id`, `pipeline_id`, `source` | narrow the list |
| `sort_by`, `sort` | default `leads.created_at` desc |

Response `{ data: [ { id, name, phone, email, chat_id, source, stage_id, pipeline_id, attributes: { <key>: <value> }, stage: { id, name }, pipeline: { id, name, phone_number }, created_at, updated_at, ... } ] }`. `attributes` is the lead's custom-attribute values keyed by attribute key, so this is also how you check whether the AI filled the attributes you configured. When ops give you only a name, search it, then confirm the phone with them before reading further. If nothing matches, the lead may live on a different pipeline than you assumed; drop the `pipeline_id` filter.

### Which channel holds the conversation

A lead's messages live in one of three places, and only the first two are readable through the API:

| Channel | How to tell | Where the history is |
|---|---|---|
| WhatsApp Business API (Cloud API) | `GET /whatsapp/accounts` has an active account and the lead's pipeline phone matches it, or `pipeline.has_api_account` is true in `/users/metadata` | `GET /whatsapp/conversations/messages` (below) |
| Hosted WAHA session | `GET /waha/sessions` lists a session whose `phone_number` matches the lead's pipeline phone | `GET /waha/sessions/{public_id}/chats/{chatId}/messages` (below), read live from the session |
| Chrome extension on the rep's own WhatsApp Web | neither of the above | not stored server-side. The only record is the LangSmith trace's `inputs.content`, which holds the conversation exactly as the AI saw it (see `langsmith.md`) |

### WhatsApp Business API conversations

`GET /whatsapp/conversations` — query `whatsapp_account_id` (required, from `GET /whatsapp/accounts`), `query` (name / phone / email), `page`, `count`, `has_unread`, `has_failed`, `has_needs_reply`, `filters` (JSON, same shape as the leads list). Returns `{ data: [ { id (lead id), name, phone, message_type, from_phone_number, content (last message, decoded), last_message_at, is_read, open_session, unread_count, last_message_status } ] }`. `open_session` tells you whether the 24-hour customer-service window is open, which decides whether a plain text or only a template can be sent.

`GET /whatsapp/conversations/messages` — query `whatsapp_account_id` and `lead_id` (both required), `page`, `count`, `timestamp` (paginate backwards from a unix time). Returns `{ data: [ { id, wm_id, type, status, to_phone_number, from_phone_number, content, message_at, mode, error_code } ] }` newest first, reactions excluded. Direction: a message whose `from_phone_number` is the business number is outbound. `status` is Meta's delivery state (sent / delivered / read / failed); `error_code` explains a failure. `GET /whatsapp/messages/{id}` returns one message with its media.

### Hosted WAHA session conversations

`GET /waha/sessions` — returns `{ data: [ { id, public_id, phone_number, display_phone_number, status, owner_name, pipeline_owner_id } ] }` for the sessions on the pipelines the logged-in user can see. Use the session whose `phone_number` is the lead's pipeline phone; `status` must be `WORKING` (anything else means the number is not currently connected, so there is nothing to read and nothing will send).

`GET /waha/sessions/{public_id}/chats` — query `q` (name or phone search), `limit` (≤200), `offset`. Returns `{ data: [ { id (chat id), name, lastMessage: { id, timestamp, fromMe, body, hasMedia, type, ack, ackName } } ], has_more }`.

`GET /waha/sessions/{public_id}/chats/{chatId}/messages` — query `limit` (≤100), `offset`, `downloadMedia` (bool). `chatId` is `<phone digits>@c.us` (the lead's phone with country code, no `+`), or the lead's stored `chat_id` when it is a `@lid` value; groups end in `@g.us`. Returns `{ data: [ { id, timestamp (unix), fromMe, from, body, type, hasMedia, media: { url, mimetype, filename } | null, participant (groups) } ] }`. `fromMe: true` is the business side (the AI or a human on the number).

These two endpoints read from the live session store on the WAHA worker, so keep them to a handful of calls per investigation and never poll them; a 500 with "Something went wrong while fetching chat messages" means the worker or session is down, not that the chat is empty. They sit behind the `premium_features` gate, so they return 403 on a free-pack org.

### Reading a conversation for a complaint

1. `GET /leads?phone=…` or `?search=…` → lead id, pipeline, stage, attributes.
2. Decide the channel from the table above and pull the last 20–50 messages.
3. Pull the matching LangSmith runs for the lead id (`langsmith.md`) to see what the AI was given and why it answered as it did; the API history shows what was sent, the trace shows why.
4. Quote the specific messages and the specific config line in your proposal. Never paste whole histories to the rep, and never include another lead's messages.

## 20. User settings (extension / WAHA numbers and email)

The switches in §13 belong to a WhatsApp **API** account. Numbers connected through the Chrome extension or a hosted WAHA session have no account row; their AI and auto-responder switches live on the **user who owns the pipeline** for that number. This endpoint reads and writes those, plus the org's email sender identity. On a paid, onboarded account everything defaults to on, so a build works without touching it; it is where "the AI is not replying on the office number" gets fixed.

### GET /users/settings

Query (optional): `user_id` to read another member (admin only), or `phone_number` to resolve the pipeline owner of that number. Omit both for the logged-in user.

```json
{
  "success": true,
  "settings": {
    "user_id": 8421, "user_name": "Rahul",
    "ai_replies_enabled": true,          // AI qualification replies on this user's numbers
    "ai_stage_shifting": true,           // AI may move leads between stages (per stage ai_switch still applies)
    "auto_create_leads": true,           // new inbound numbers become leads
    "auto_responder_enabled": true,      // sequences run
    "auto_responder_hours": { "start": "09:00", "end": "20:00" },
    "auto_responder_auto_pause": true,
    "auto_responder_pause_delay": { "value": 2, "unit": "hours" },
    "bump_up_enabled": true, "max_bump_up": 3,
    "af_personalization_enabled": false, // LLM rewrites each auto-followup per lead
    "from_name": "Fix My Hair", "reply_to": "care@fixmyhair.example",   // org-level, used by email sequences
    "limit": { "is_limit_active": true, "messages_limit": 200, "used_today": 12, "next_upgrade_at": null },
    "weekly_report_unsubscribed": false,
    "notifications": { "new_lead": false },
    "organization": { "fixed_bump_ups": false, "bump_up_variation_enabled": false, "bump_up_steps": [] }
  }
}
```

### POST /users/settings

All fields `sometimes`; send only what you change. Admins may pass `user_id` to configure another member; non-admins get `403 "Access denied. Admin privileges required to update other users' settings."`.

| Field | Notes |
|---|---|
| `user_id` | target member (admin only). Omit for self |
| `ai_replies_enabled` | AI qualification replies |
| `ai_stage_shifting` | let the AI move leads between stages |
| `auto_create_leads` | create leads for unknown inbound numbers |
| `auto_responder_enabled` | run auto-followup sequences. Turning it off also cancels the user's pending scheduled messages |
| `auto_responder_hours` | JSON **string** `{"start":"09:00","end":"20:00"}`, 24h `HH:MM` |
| `auto_responder_auto_pause` | pause a sequence when the lead replies |
| `auto_responder_pause_delay` | JSON **string** `{"value":2,"unit":"hours"}`; `unit` in `minutes`\|`hours`\|`days`, value ≥1, max 7 days (168 h / 10080 min) |
| `bump_up_enabled`, `max_bump_up` | nudges on / off; 1–5 |
| `af_personalization_enabled` | LLM-personalised auto-followups |
| `from_name` (≤255), `reply_to` (email) | **org-level** sender identity for `email` sequence messages; required before any email sequence goes out |
| `is_limit_active` | daily message cap on / off (the cap itself is set by Kraya) |
| `weekly_report_unsubscribed` | weekly email report opt-out |

```json
{
  "user_id": 8421,
  "ai_replies_enabled": true,
  "ai_stage_shifting": true,
  "auto_responder_enabled": true,
  "auto_responder_hours": "{\"start\":\"09:00\",\"end\":\"20:00\"}",
  "auto_responder_auto_pause": true,
  "auto_responder_pause_delay": "{\"value\":2,\"unit\":\"hours\"}",
  "max_bump_up": 3,
  "from_name": "Fix My Hair",
  "reply_to": "care@fixmyhair.example"
}
```

Response `{ success: true, message: "User settings updated." }`. Validation failures return `400 { message: "Bad Request", errors }`.

Which switch wins: a WhatsApp API number uses the account row (§13); an extension / WAHA number uses its pipeline owner's settings here; a stage with `ai_switch: false` silences the AI in that stage regardless of either.

---

## 21. Conversational operating protocol (existing accounts)

The Agent Hub update agent runs under these rules. Adopt them verbatim.

1. **Disambiguate** before any call when the request is unclear (which sequence, which message, replace or edit).
2. **Discover**: call the relevant list endpoint; use real ids and current values, never memory.
3. **Propose**: show `Current:` / `Proposed:` / `Proceed?` and wait for an explicit yes. Once the rep confirms, the next action is the single mutation call, no re-explaining.
4. **Mutate** one entity per turn. Report `1/N done — <name>` and continue next turn for bulk work.
5. **Deletes**: quote the entity and what is lost, ask "Are you sure?", then require the literal `confirm delete`. "wait" / "stop" / "cancel" aborts.
6. **Edits merge, never overwrite**: fetch the current entity first. For rules, dropdown attributes and org info, copy everything, change only the target, send the whole thing back. For a sequence, send only the messages you change, each with its `id`; removed messages need an explicit `DELETE /auto-responder/sequence-message` (§8). Distinguish "edit the day-2 message" from "replace the sequence" (delete the old messages, then create the new ones) and ask if unsure.
7. **New content**: draft the sequence / FAQ / rule from the rep's brief, show it in plain language, then push on approval. For rules, fetch sequences, stages and pipelines first so cross-reference ids are real.
8. **Stages**: read `/users/metadata`, place new stages after Qualified, never rename New Lead / Qualified (everything else on any stage is editable), give every stage a description, pick colours yourself.
9. **Templates**: `GET /whatsapp/accounts` first; `GET /whatsapp/templates?count=1000` to reconcile names; tell the rep submission means PENDING until Meta replies.
10. **Plan**: for anything over ~3 steps keep a checklist and tick items as they complete so a trimmed context cannot cause double work.
11. **Out of scope**: billing, users, phone-number changes, anything needing Kraya admin permissions. Say so and point the rep to the Kraya dashboard.

---

## 22. Tool → endpoint map

| Tool | Call |
|---|---|
| bootstrap / login | `POST /auth/login` → `GET /users/metadata` |
| list_pipelines | `GET /pipelines` |
| list_stages / upsert_stage / delete_stage | `GET /stages`, `POST /stages`, `DELETE /stages/{id}` (default-stage check via `GET /users/metadata`) |
| list_attributes / upsert_attribute / delete_attributes | `GET`, `POST`, `DELETE /custom-attributes` |
| list_sequences / get_sequence / upsert_sequence / delete_sequence / delete_sequence_message | `GET /auto-responder/sequences`, `POST /auto-responder/sequence`, `DELETE /auto-responder/sequence`, `DELETE /auto-responder/sequence-message` |
| update_org_info | `POST /organizations/info` (read current from `GET /users/metadata` → `user.organization.info`) |
| list_faqs / list_faq_categories / upsert_faq / update_faq_category / delete_faq / delete_faq_category | `GET /faqs/articles`, `GET /faqs/categories`, `POST /faqs/articles`, `PUT /faqs/categories/{id}`, `DELETE /faqs/articles`, `DELETE /faqs/categories/{id}` |
| list_rules / get_rule / upsert_rule / delete_rule / reorder_rules | `GET /rules`, `POST /rules`, `DELETE /rules/{id}`, `POST /rules/reorder` |
| list_quick_replies / upsert_quick_reply / delete_quick_reply / reorder_quick_reply_groups / reorder_quick_replies | `GET`, `POST /quick-replies`, `DELETE /quick-replies/{id}`, `POST /quick-replies/groups/reorder`, `POST /quick-replies/reorder` |
| list_whatsapp_accounts | `GET /whatsapp/accounts` |
| list_templates / get_template / create_template / delete_template | `GET /whatsapp/templates?count=1000`, `GET /whatsapp/templates?template_id=`, `POST /whatsapp/template`, `DELETE /whatsapp/template/{id}` |
| *(new)* update_whatsapp_account | `POST /whatsapp/accounts` |
| *(new)* upload_file | `POST /upload` |
| *(new)* update_bumpups | `POST /organizations/bumpups` (read from `GET /users/metadata`) |
| *(new)* get/update_calendar, create/delete_reminder_sequence | `GET/PUT /calendar/settings`, `POST /calendar/settings/logo`, `POST/DELETE /calendar/settings/reminder-sequence` |
| *(new)* get/update_copilot_config | `GET/PATCH /organization-config` |
| *(new)* get/update_user_settings | `GET/POST /users/settings` (admin may pass `user_id`) |
| *(new)* find_lead / list_conversations / get_conversation_messages | `GET /leads`, `GET /whatsapp/conversations`, `GET /whatsapp/conversations/messages`, `GET /waha/sessions`, `GET /waha/sessions/{public_id}/chats`, `GET /waha/sessions/{public_id}/chats/{chatId}/messages` (read-only, §19) |
| *(new)* test_ai_reply / test_qualification / test_bumpup | `POST /demo/ai-chat/generate-reply`, `POST /demo/ai-chat/check-qualification`, `POST /demo/ai-chat/generate-bumpup`, `GET /demo/ai-chat/organization-info` (public, by org slug) |

Rows marked *(new)* have no Agent Hub tool yet; the cloud agent adds them. The Agent Hub agents also skip `whatsapp_template` / `ai_call` sequence messages and the `send_template_message`, `set_lead_attribute` and `schedule_message` rule actions; this doc covers them (§8, §11) so the cloud agent can use them when the org has a WhatsApp API account.

Deliberately out of scope: signup and onboarding, pack changes, adding team members (`/organizations/invite`, signup with `organization_id`), `POST /pipelines` and round robin, message queue priority, product catalog, website widget, lead ingestion and lead edits (`POST /leads`, imports, sending messages), integrations, connecting WhatsApp API or Instagram accounts, billing.

---

## 23. Ops CRM write-back (commitments and integrations)

A different service from everything above: the **Kraya Ops CRM** at `https://ops.kraya-ai.com`, the board ops runs the client on. One endpoint, so the promises made on a call and the integrations agreed on it become tracked work instead of prose in a transcript.

```
POST https://ops.kraya-ai.com/api/hooks/agent
Authorization: Bearer $OPS_CRM_AGENT_TOKEN
Content-Type: application/json
```

```json
{
  "orgId": "a2a9d326-b1a3-41e8-ae41-d24b824a28c8",
  "source": "brainstorming call 3 Sep",
  "tasks": [
    { "title": "Import the client's 8,000 existing contacts", "details": "Promised on the 3-Sep call: \"we'll get your old contacts in\". No deadline given." },
    { "title": "Run the promised training session for the team", "assignee": "Nishtha" }
  ],
  "integrations": [
    { "type": "indiamart" },
    { "type": "whatsapp api", "assignee": "Devang" }
  ]
}
```

**The token** is `OPS_CRM_AGENT_TOKEN` in your environment, or an `ops_…` token the rep pastes. Without one, do not drop the commitments: list them in the handover and tell the rep they need to go on the card by hand.

**Identifying the client** — send exactly one of `orgId` (the Kraya org id, what you already have from `/users/metadata`), `krayaLeadId`, or `clientId` (the Ops CRM's own id). A `404` means the client has no card on the board, or its card has no org id recorded; report that to the rep rather than retrying, because the fix is theirs.

**Fields**

| Field | Meaning |
|---|---|
| `source` | free text, stored as the task's author (`agent: <source>`). Name the call and its date. |
| `tasks[].title` | one commitment, one task. The promise in a few words — the team reads this on a list next to other clients' tasks. |
| `tasks[].details` | the commitment verbatim with its date and deadline, so whoever picks it up sees what was actually said. |
| `tasks[].assignee` / `integrations[].assignee` | team member's first name. Omit and it goes to the client's POC. |
| `integrations[].type` | lowercase, matching what the board already uses: `meta lead ad`, `whatsapp api`, `justdial`, `indiamart`, `zoho`, `google sheets`, `neodove`, `capi`, … |

**Idempotent.** A task is keyed on the client plus the promise text, an integration on the client plus its type, so re-analysing the same call creates nothing new. The response says exactly what happened:

```json
{ "client": { "id": "…", "name": "Rishabh Overseas x Kraya AI ( DFY 4 months )", "poc": "Nishtha" },
  "created": { "tasks": ["Import the client's 8,000 existing contacts"], "integrations": ["indiamart"] },
  "skipped": { "tasks": ["Run the promised training session for the team"], "integrations": [] } }
```

Because it is keyed on the text, a reworded promise creates a second task. Keep titles stable across runs, and when you are unsure whether a commitment is already tracked, read the card first rather than sending a variant.

**This endpoint never closes anything.** Tasks are completed by the ops member who did the work, in the Ops CRM. Your job is to raise them and, at handover, report which are still open.
