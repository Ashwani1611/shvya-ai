# Kraya API powers: every endpoint the agent can call on a Kraya CRM account

A digest of `.claude/skills/kraya-account-setup/references/api-reference.md` (the contract verified against the Kraya Laravel validators). It lists what the agent can read, write and delete on a client's account, the traps per endpoint, the behavioural rules the doc states, and what the agent must never do. The source file stays authoritative; its field tables are never edited from memory. The companion explanation of how the agent works is `docs/kraya-account-agent-guide.md`.

## 1. Auth and conventions

| Item | Value |
|---|---|
| Base URL | `https://api.kraya-ai.com/api` (staging `https://api-staging.kraya-ai.com/api`); from `KRAYA_API_BASE_URL` |
| Auth header | `Authorization: Bearer <token>` on every call except `POST /auth/login` (and the public `/demo/ai-chat/*` endpoints, where sending it is optional but raises the rate limit) |
| Content type | `Content-Type: application/json` for every non-GET call, except the two multipart uploads (`POST /upload`, `POST /calendar/settings/logo`) |
| Token source | `token` field of the `POST /auth/login` response (plus `token_expires_at`). Passport personal-access tokens. Per CLAUDE.md, the rep normally pastes a 24-hour token generated from the SuperAdmin analytics view; credential login is the fallback |
| Token lifetime / refresh | On a `401`, re-run `POST /auth/login` and replay the request once. Tokens never go to files, logs, commits or replies |
| Role | Use an org `admin` token. `admin-only` middleware guards: sequences and sequence messages, pipelines, rule writes, org info, bump-ups, catalog writes, sendable templates, round-robin writes, calendar settings. Stages, custom attributes, FAQs, quick replies, `GET /rules` and `/whatsapp/*` need only a logged-in org user (templates and WhatsApp settings also need a premium pack) |
| Pack | Calendar is Basic/Pro only. WhatsApp API features need a connected account. Free-tier caps (5 sequences, 10 attributes, 10 quick replies, 20 stages) do not apply on a paid pack. WAHA chat reads sit behind `premium_features` (403 on free) |
| Pagination | List endpoints paginate at `count=10` by default (`/stages`, `/faqs/articles`, `/faqs/categories`, `/whatsapp/templates`). Pass `count=1000` for templates; read stages from `/users/metadata` (unpaginated). `GET /leads` requires both `page` and `count`. `GET /rules` and `GET /auto-responder/sequences` are unpaginated. WAHA chats: `limit` ≤200 / `offset`; WAHA messages: `limit` ≤100 / `offset` |
| Org confirmation | After login, confirm `GET /users/metadata` returns the org you logged into; stop if a platform-injected header has redirected calls to the ops org |

### Response envelopes (inconsistent; read exactly)

| Endpoint family | Success shape | Error shape |
|---|---|---|
| auth, metadata, upload | flat object (`token`, `user`, `pipelines`, `url`, …) | `{ message, errors }` |
| stages, pipelines, faqs | `{ data: [...] }` on lists; created entity as flat object on create | `{ message, error }` |
| sequences, rules, templates, whatsapp accounts, calendar, bump-ups | `{ success, message, data }` (bump-ups: `organization`) | `{ success:false, message, error\|errors }` |
| custom attributes | `{ success, message, attributes: [...] }` | `{ success:false, message, errors }` |
| quick replies | nested object keyed by group name then reply key; create returns the reply | `{ message, error }` |

### Status codes

`400` validation, `401` bad/expired token, `403` not admin / pipeline not yours / pack gate, `404` referenced entity missing or belongs to another org, `409` duplicate (stage slug, calendar reminder sequence, sequence message name/content), `422` inactive WhatsApp account / unreadable catalog file / duplicate sendable template / bump-up validation, `500` server error.

### The "full replacement on upsert" rule (where it appears)

- General rule (§3.2): most upserts replace the whole field they receive. To change one item: fetch, merge, send the full list back.
- `POST /custom-attributes`: `values[]` on a dropdown/multi_select replaces the whole option list.
- `POST /rules`: `trigger_conditions[]` and `attribute_conditions[]` replace on update.
- `POST /organizations/info`: full replace of `org_name`, `about`, `qualification_requirements`, `attachments`, `bot_languages`, `sendable_files`; `about` and `bot_languages` are blanked if omitted.
- `POST /organizations/catalog/{id}/filters`: the `filters` array replaces any earlier mapping.
- `POST /organizations/catalog`: replaces (deletes) the org's existing catalog.
- **Exception:** `POST /auto-responder/sequence` `messages[]` is upserted per message by `id`; messages left out are kept, never deleted.
- Upserts key on `id` being *present*, not non-null: never send `"id": null`; omit the key to create.

### Two flows

- **Flow A (first build, order matters):** login → metadata → attributes → stages → WhatsApp accounts/templates (wait for APPROVED) → sequences → upload + org info (→ catalog) → FAQs → rules → quick replies → bump-ups → calendar (+ reminder sequence) → Co-Pilot config → user settings.
- **Flow B (existing account):** login, then for every change **discover → propose → confirm → mutate**, one entity per turn.

## 2. Endpoint table

Legend: R = read, W = write, D = delete. "Dangerous notes" are the doc's own warnings.

### Auth and metadata

| Method | Path | What it does | Key fields / constraints | R/W | Dangerous notes |
|---|---|---|---|---|---|
| POST | `/auth/login` | Only entry point; returns full metadata + `token`, `token_expires_at` | `{ email, password }`; `401 "Invalid Credentials"` | R (auth) | `POST /auth/signup` and SuperAdmin one-time links are out of scope |
| GET | `/users/metadata` | Canonical snapshot: user, org (`id` UUID, `name`, `slug`, `pack`, `timezone`, `industry`, `info` = org info, bump-up config, `calendar_config`, `onboarding_completed`), `pipelines[]` with `stages[]`, `attributes[]`, `has_whatsapp_business_account`, `quick_replies` | Pick pipeline with `pipeline_type = "leads"` (fallback: first); record every seeded funnel stage id by slug; `is_default: true` marks the 6 inbox views, not funnel stages | R | Confirm the org name here before writing anything; this is the only read for org info and bump-ups |

### Pipelines

| Method | Path | What it does | Key fields / constraints | R/W | Dangerous notes |
|---|---|---|---|---|---|
| GET | `/pipelines` | List pipelines with stages | Query `id`, `page`, `count` | R | Prefer `/users/metadata` for the leads pipeline |
| POST | `/pipelines` | Create/update a pipeline | `{ id \| name, order, pipeline_owner_id?, phone_number? }` | W | **Not used by the agent**; multi-pipeline accounts are set up by ops in the UI (declared out of scope) |

### Stages

| Method | Path | What it does | Key fields / constraints | R/W | Dangerous notes |
|---|---|---|---|---|---|
| GET | `/stages` | List stages | Query `page`, `count` (default 10), `id` | R | Paginated; prefer `/users/metadata` |
| POST | `/stages` | Create or update a stage | `pipeline_id` required always; `id` for update (omit to create); `name` (slug derived, unique per pipeline); `description` ≤255; `color_code` hex (blue in-progress, amber action-needed, green won, red lost, purple parked); `order` (must be > Qualified's order for new stages; always send on create so `message_queue_priority_order` populates); `is_hidden`; `ai_switch`; `required_attribute_keys[]` | W | `409 "Stage Already Created."` when slug exists (reuse id from metadata). Never rename New Lead / Qualified. Every stage gets a description |
| DELETE | `/stages/{id}` | Delete a stage | Only non-default, empty stages | D | Refuses default stages (`is_default = 0` scope); `400 "Stage has leads"` (move leads first). Needs `confirm delete` |

### Custom attributes

| Method | Path | What it does | Key fields / constraints | R/W | Dangerous notes |
|---|---|---|---|---|---|
| GET | `/custom-attributes` | List attributes | Returns `{ success, attributes: [ { id, key, data_type, values, description, has_linked_rules } ] }` | R | |
| POST | `/custom-attributes` | Create or update, keyed by `key` | `key` regex `^[a-zA-Z0-9 _./&-]+$` ≤150 (existing key ⇒ update); `data_type` ∈ text, number, date, datetime, dropdown, multi_select; `values[]` (dropdown/multi_select, max 50, extras silently dropped); `description` ≤1000 (treat as required); `new_key_name` to rename | W | **`values[]` replaces the whole option list.** Changing `data_type` or `values` starts a background cleanup of lead values (`sync_status: "running"`); poll `sync-status` before writing again. Free tier returns an upgrade payload |
| DELETE | `/custom-attributes` | Delete attributes | Body `{ "attributes": "{\"Key A\":true,\"Key B\":true}" }` (JSON **string** keyed by attribute key) | D | Needs `confirm delete` |
| GET | `/custom-attributes/sync-status` | Cleanup status after a type/values change | `{ status: "running" \| "idle" }` | R | Wait for `idle` before writing the attribute again |

### Sequences and sequence messages (auto-responder)

| Method | Path | What it does | Key fields / constraints | R/W | Dangerous notes |
|---|---|---|---|---|---|
| GET | `/auto-responder/sequences` | List sequences with full message bodies | Query `sequence_id`, `query`, `include_reminders`; `enabled` accepted but has no effect. Unpaginated | R | Never read or report `enabled` |
| POST | `/auto-responder/sequence` | Create or update a sequence and upsert its messages | `id` (update) or `sequence_name` (create, ≤255, unique per org); `description` ≤500; `mode` ∈ extension, whatsapp_api (required); `whatsapp_account_id` when `whatsapp_api` (active id only); `messages[]` each with `id?`, `message_name` (unique in sequence, ≤255), `message_type` ∈ whatsapp, email, reminder, whatsapp_template, ai_call, `subject` (email, unique), `content` (all but template; `{lead_first_name}` only), `message_delay` (immediate / after_x_units / before_x_units / specific_time / recurring; unit minutes\|hours\|days, delay ≥1; `recurring` not allowed on ai_call), `order` (1-indexed), `files[] {url,name}`, `send_as_caption`, `template_id` (APPROVED, same number as sequence), `template_retry_limit` 0–5, `call_retry_limit` 1–10, `call_retry_interval` ≥1, `call_retry_interval_unit` minutes\|hours, `call_outcome_instructions` ≤5000 | W | **Messages are upserted by `id`, never replaced; omitted messages are kept, never deleted.** Re-sending an existing message without `id` fails with 409 and rolls back the whole update. `order` is not renumbered. `mode` and `whatsapp_account_id` are written only on create (validated and ignored on update). `whatsapp` messages only on `extension`, `whatsapp_template` only on `whatsapp_api`. Rejects near-duplicate content. Read id from `data.id`. Read back after every change |
| POST | `/auto-responder/sequence/duplicate` | Clone a sequence | `{ id }` | W | The way to move a sequence between extension and API or to another number (then reassign rules) |
| DELETE | `/auto-responder/sequence` | Delete a sequence | Body `{ "id": 4812 }` (body, not path) | D | Needs `confirm delete` |
| DELETE | `/auto-responder/sequence-message` | Delete one message | Body `{ "id": 19201 }` (body, not path) | D | The only way to remove a message. Needs `confirm delete` |

### Organization info (the AI's memory / knowledge base)

| Method | Path | What it does | Key fields / constraints | R/W | Dangerous notes |
|---|---|---|---|---|---|
| GET | `/users/metadata` → `user.organization.info` | Read current org info (no dedicated GET) | `about`, `qualification_requirements`, `attachments[]`, `bot_languages`, `sendable_files[]` | R | Read before every write |
| POST | `/organizations/info` | **Full replace** of org info; re-indexes `about` + qualification into the vector store asynchronously | `org_name` and `about` required every call; `qualification_requirements` (markdown with up to 8 `##` sections: Rules, Welcome Message, Qualification Questions, Attribute & State Mapping, Stage Shifting Logic, Edge Cases, Final Acknowledgment Message, Qualification); `attachments` JSON **string** of `[{url,name}]` (URLs scraped into Pinecone; always include website); `bot_languages` comma-separated ≤500 (send always, default `"English"`); `sendable_files` JSON string of `[{ id (uuid), url, name, type: document\|image\|video, description 1–500, created_at? }]`, per-pack limit (`400 "Sendable files limit exceeded…"`) | W | Replaces every field; `about` and `bot_languages` blanked if omitted. Attribute names/values and stage names inside `qualification_requirements` must match real attributes and stages exactly |

### FAQs

| Method | Path | What it does | Key fields / constraints | R/W | Dangerous notes |
|---|---|---|---|---|---|
| GET | `/faqs/categories` | List categories | Query `page`, `count` (default 10), `id`, `include_empty`; returns `{ id, title, description, article_count }` | R | Paginated |
| GET | `/faqs/articles` | List articles | Query `page`, `count` (default 10), `category_id`, `id` | R | Paginated |
| POST | `/faqs/articles` | Create or update an article (vector-indexed on save) | `id` (update); `category_id` or `category_name` (find-or-create) + `category_description`; `title` (question) and `content` (answer, 2–4 specific sentences) required; `attachments[]` | W | Facts from client materials only; write a hand-off when the source lacks the answer |
| DELETE | `/faqs/articles` | Delete one article | Body `{ "id": 3410 }`, one per call | D | Needs `confirm delete` |
| PUT | `/faqs/categories/{id}` | Rename/describe a category | `{ title (required, ≤255), description }` (note `title`, not `name`) | W | |
| DELETE | `/faqs/categories/{id}` | Delete category | — | D | **Deletes the category and all its articles.** Needs `confirm delete` |

### Rules (smart triggers)

| Method | Path | What it does | Key fields / constraints | R/W | Dangerous notes |
|---|---|---|---|---|---|
| GET | `/rules` | List rules (full objects, unpaginated) | Query `search`; returns `id, name, description, trigger_type, action_type, enabled, processing_order, trigger_conditions, attribute_conditions, keyword_trigger_keywords, no_response_duration_value, action_stage, action_pipeline, action_sequence, action_rr_pipelines, …` | R | `rules.enabled` is the real gate on whether a sequence runs |
| POST | `/rules` | Create or update a rule | `rule_id` for update (**not `id`**); `name` ≤255, `description`, `trigger_type`, `action_type` required; `enabled` default true; `trigger_conditions[]` min 1, OR'd, `{ condition_stage_id, condition_pipeline_id, condition_sequence_id? }`; `attribute_conditions[]` AND'd `{ attribute_key, match_type: equals\|contains, match_values[] }`. **Triggers:** `lead_moved_to_stage`, `new_lead_created`, `no_response_from_lead` (+`no_response_duration_value` ≥1, ≥5 if minutes; `_unit` minutes\|hours), `keyword_detected` (+`keyword_trigger_keywords[]` 1–50, `"*"` = any), `days_in_stage` (+`days_in_stage_value`, `_unit` hours ≤17520 \| days ≤730), `call_logged` (+`trigger_call_status` done\|no_response\|missed), `sequence_completed` (all three condition ids, one condition per stage), `call_slot_booked`. **Actions:** `move_to_stage` (`action_stage_id`, `action_pipeline_id`, stage must belong to pipeline), `initiate_sequence` (`action_sequence_id`, `action_overwrite_sequence?`), `stop_assigned_sequence`, `set_call_reminder` (`action_call_reminder_value` ≥1, caps 730 d / 17520 h / 1051200 min, `_unit`, `_note` ≤2000, `_overwrite`), `toggle_ai` / `toggle_auto_followup` (`action_value` on\|off), `round_robin_assignment` (`action_rr_pipeline_ids[]` ≥1, Sales pipelines with RR enabled, own org; not with `no_response_from_lead`), `send_email` (`action_email_subject` ≤200, `_body` ≤100000, `_send_to` lead\|custom, `action_email_to[]` ≤10 distinct), `send_template_message` (`action_whatsapp_account_id`, `action_template_id` APPROVED on that account, every variable needs an example; sends immediately), `set_lead_attribute` (`action_attribute_key_id` numeric id, `action_attribute_value` validated by data_type: text ≤10000, number, date `DD-MM-YYYY`, datetime `DD-MM-YYYY HH:mm:ss`, dropdown ∈ values, multi_select comma-joined ⊆ values), `schedule_message` (`action_message_type` text\|text_api\|template; `action_message_body` ≤4096; template needs account + template id, `action_message_max_retries` ∈ {0,1,2,3,5}, `action_message_retry_after_hours` ∈ {8,12,16,24,48} when retries > 0; `action_schedule_time_type` fixed_time `HH:MM` \| relative value+unit (same caps as call reminders) \| from_attribute (`action_schedule_attribute_key_id`, must be `datetime`)) | W | **`trigger_conditions[]` / `attribute_conditions[]` replace on update.** Send only the action's own fields; stray ones are rejected. Identical rules rejected (`identical_rule`): treat as skip. Read id from `data.id`. `text_api` only delivers inside the 24h window. Never create intent-keyword → Qualified rules |
| DELETE | `/rules/{id}` | Delete rule | `{ success, message: "Rule deleted successfully" }` | D | Needs `confirm delete` |
| POST | `/rules/reorder` | Set processing order | `{ rule_orders: [ { rule_id, processing_order } ] }`; all matching rules fire in `processing_order` | W | |

### Quick replies

| Method | Path | What it does | Key fields / constraints | R/W | Dangerous notes |
|---|---|---|---|---|---|
| GET | `/quick-replies` | List replies as nested object keyed by group then reply key | Query `category`, `key`; skip keys starting with `_` (`_group_id`, `_group_order`). Kraya seeds 4 defaults | R | Not an array |
| POST | `/quick-replies` | Create or update | `id` (update); `name` (operator-facing label), `key` (slash command, unique per org, ≤100), `category` (group name, find-or-create), `content` (`{lead_name}`, `{user_name}`, `{org_name}`) required; `attachments` JSON **string** of `[{url,name}]` | W | Duplicate name/key ⇒ treat "already exists" as skip |
| DELETE | `/quick-replies/{id}` | Delete reply | `{ message: "Quick reply deleted" }` | D | Needs `confirm delete` |
| POST | `/quick-replies/groups/reorder` | Reorder groups | `{ group_orders: [ { group_id, order } ] }` | W | |
| POST | `/quick-replies/reorder` | Reorder replies | `{ quick_reply_orders: [ { quick_reply_id, order } ] }` | W | |

### WhatsApp API accounts and templates

| Method | Path | What it does | Key fields / constraints | R/W | Dangerous notes |
|---|---|---|---|---|---|
| GET | `/whatsapp/accounts` | List connected WABA accounts and their switches | Returns `id, name, display_phone_number, phone_number, phone_number_id, status, ai_replies_enabled, auto_create_leads, bump_up_enabled, max_bump_up, auto_responder_enabled, auto_responder_hours, auto_responder_auto_pause, auto_responder_pause_delay, new_lead_broadcast_enabled, new_lead_broadcast_template_id, new_lead_broadcast_template, request_contact_info_template_id, request_contact_info_template, affected_sequences_count, mm_lite_active, coexistence_enabled` | R | Usable only when `status === "active"`. Empty list ⇒ no WABA. **Never guess a `whatsapp_account_id`** (Kraya accepts a foreign id, saves, then 500s) |
| POST | `/whatsapp/accounts` | Configure an already connected account (all fields `sometimes`) | `whatsapp_account_id` required; `ai_replies_enabled`, `auto_create_leads` (or `auto_create_lead`), `bump_up_enabled`, `max_bump_up`, `auto_responder_enabled`, `auto_responder_hours` JSON **string** `{"start":"09:00","end":"20:00"}`, `auto_responder_auto_pause`, `auto_responder_pause_delay` JSON string `{"value":2,"unit":"hours"}` (minutes\|hours\|days), `new_lead_broadcast_enabled`, `new_lead_broadcast_template_id` (APPROVED on this account; `null` clears), `request_contact_info_template_id` (`null` clears) | W | Connecting the account is out of scope. `400 "Invalid template. Template must be approved and belong to this WhatsApp account."` |
| GET | `/whatsapp/templates` | List templates | Query `template_id`, `status[]` (DRAFT\|PENDING\|APPROVED\|REJECTED), `categories[]` (MARKETING\|UTILITY\|AUTHENTICATION), `whatsapp_account_id[]`, `query`, `page`, `count` (default 10, **always pass 1000**), `sort_by`, `sort_order`. `whatsapp_account_id` is a string: compare with `Number()` | R | Check here before creating: Kraya does not de-duplicate |
| POST | `/whatsapp/template` | Create (and submit to Meta) a template | `whatsapp_account_id` (active), `display_name` ≤512, `category` MARKETING\|UTILITY (carousel must be MARKETING), `status` `"DRAFT"`, `submit` (true ⇒ PENDING; false saves draft), `components[]` (HEADER text with `example.header_text` or IMAGE/VIDEO/DOCUMENT with `example.header_handle` + `attachment_file_name`; BODY with named `{{var}}` placeholders + `example.body_text` one per var in first-appearance order; FOOTER; BUTTONS: QUICK_REPLY, URL (+example), PHONE_NUMBER, COPY_CODE; CAROUSEL 2–10 cards, same button types in same order, no top-level header/footer), `id` for update. Button text ≤25, header/footer ≤60; variable cannot be first or last token of body; PHONE_NUMBER cannot sit with QUICK_REPLY | W | Duplicate `display_name` silently creates `"<name> v2"`. Approval is asynchronous (Meta webhook flips to APPROVED/REJECTED with `rejection_reason`); poll `GET /whatsapp/templates?template_id=`. `422 "The specified WhatsApp account does not belong to your organization or is not active."` |
| DELETE | `/whatsapp/template/{id}` | Soft-delete template | — | D | Detaches from sequences; **sequences left with zero messages are soft-deleted too.** Needs `confirm delete` |
| GET | `/organizations/sendable-templates` | List templates the AI may send mid-chat | `{ success, data: [ { id, whatsapp_template_id, description, ... } ] }` + whether still usable | R | Cloud API accounts only; admin-only |
| POST | `/organizations/sendable-templates` | Add a sendable template | `{ whatsapp_template_id (APPROVED, this org, active account), description ≤500 }`; `201`; same template twice → `422` | W | Admin-only. Description is the "when to send / when not"; template must stand alone (model leaves `message` empty); never more than one per turn |
| PUT | `/organizations/sendable-templates/{id}` | Edit the trigger description | `{ description }`; `404` if not this org's | W | Only description is editable |
| DELETE | `/organizations/sendable-templates/{id}` | Remove sendable template | — | D | Needs `confirm delete` |

### File upload

| Method | Path | What it does | Key fields / constraints | R/W | Dangerous notes |
|---|---|---|---|---|---|
| POST | `/upload` | Multipart upload to org's S3 folder | `file` required (keep to a few MB); `folder` only `faqs` accepted (omit otherwise). Returns `{ url }` (public, `kraya-files.s3.ap-south-1.amazonaws.com/<org-id>/<hash>.<ext>`) | W | Use the URL in org `attachments`, `sendable_files`, sequence `files[]`, quick reply/FAQ attachments, template media headers, catalog `url`. Upload only when the file is not already online |

### Bump-ups

| Method | Path | What it does | Key fields / constraints | R/W | Dangerous notes |
|---|---|---|---|---|---|
| GET | `/users/metadata` → `user.organization.fixed_bump_ups`, `bump_up_variation_enabled`, `bump_up_steps[]` | Read bump-up config | — | R | No dedicated GET |
| POST | `/organizations/bumpups` | Set AI vs fixed bump-ups and their steps | `fixed_bump_ups` required (true = fixed steps, false = AI-written; stored steps kept); `bump_up_variation_enabled` (LLM rewords each fixed step per lead); `bumpups[]` 1–5 of `{ sort_index 0–4, message ≤4000 (`{lead_first_name}`), delay_minutes 1–10080 }`; omit `bumpups` to flip mode without touching steps | W | Switching to fixed on an org with no steps seeds a default schedule. Whether/how many fire is set by `bump_up_enabled` / `max_bump_up` (max 5) on the account or user. `422 { message: "Validation failed", errors }` |

### Calendar booking (Basic/Pro only)

| Method | Path | What it does | Key fields / constraints | R/W | Dangerous notes |
|---|---|---|---|---|---|
| GET | `/calendar/settings` | Read booking page config (`/book-a-call/<org_slug>`, `/calendar/<calendar_slug>`) | Returns `id, enabled, calendar_slug, logo_url, title, sub_heading, description, key_highlights[], session_title, calendar_content, discussion_points[], show_session_details, confirmation_*, show_booking_details, show_add_to_calendar, send_calendar_invite_to_lead, redirect_*, form_fields, available_days, working_hours_start/end, max_days_open, minimum_notice_minutes, slot_interval_minutes, slots_per_interval, max_slots_per_day, blackout_time_ranges[], block_specific_dates[], block_weekdays[], reminder_sequence_id` | R | `403 "Calendar is available on Basic and Pro plans"` on free org |
| PUT | `/calendar/settings` | Update booking page (all `sometimes`) | `enabled`, `calendar_slug` `[a-z0-9-]` ≤255; page copy (`title`, `sub_heading` ≤255, `description`, `key_highlights[]` ≤6, `session_title`, `calendar_content`, `discussion_points[]`, `show_session_details`, `logo_url`); confirmation (`confirmation_heading/content`, `show_booking_details`, `show_add_to_calendar`, `send_calendar_invite_to_lead`, `redirect_enabled`, `redirect_cta_text`, `redirect_url`); availability (`available_days` list of day names or `[{day,from,to}]`, `working_hours_start/end` `H:MM`, `max_days_open` 1–365, `minimum_notice_minutes` ≥0, `slot_interval_minutes` 5–480, `slots_per_interval` 1–50, `max_slots_per_day` 1–500, `blackout_time_ranges[] {start,end}`, `block_specific_dates[]` `YYYY-MM-DD`, `block_weekdays[]`); `form_fields` `{ fields: [ { label, attribute_key, type small_text\|number\|date\|datetime\|select, required, order, values, placeholder } ], settings: { accent_color, submit_button_text, consent_required, consent_text, form_submission_email } }` | W | Send only what you change. Form `type` follows the attribute's data type; name/phone/email are built in, not part of `fields` |
| POST | `/calendar/settings/logo` | Upload logo (multipart `file`, png/jpg/jpeg/svg ≤2 MB) | Sets `logo_url` | W | |
| POST | `/calendar/settings/reset` | Reset calendar to defaults | — | W | Destructive to calendar config |
| POST | `/calendar/settings/reminder-sequence` | Create an empty *reminder* sequence linked to the calendar | `{ sequence_name, description?, mode: extension\|whatsapp_api, whatsapp_account_id? }`; `409 "A reminder sequence already exists for this calendar."`. Add messages via `POST /auto-responder/sequence` with returned `sequence.id`; delays anchor to the booked slot (`before_x_units`, `after_x_units`, `specific_time` {hours, minutes}, `immediate`); `{{booked_slot}}` variable renders slot in org timezone | W | |
| DELETE | `/calendar/settings/reminder-sequence` | Detach and soft-delete the reminder sequence | — | D | Needs `confirm delete` |

### Co-Pilot / organization config

| Method | Path | What it does | Key fields / constraints | R/W | Dangerous notes |
|---|---|---|---|---|---|
| GET | `/organization-config` | Read Co-Pilot config | `{ config: { copilot_enabled, copilot_call_tracking_start_at, copilot_activation_at, copilot_intent_threshold, copilot_followup_threshold, copilot_exempt_stages, copilot_hot_lead_stages, copilot_thresholds } }` | R | Timestamps and `copilot_thresholds` are Kraya-managed |
| PATCH | `/organization-config` | Update Co-Pilot settings | Only 5 writable keys: `copilot_enabled` (master switch), `copilot_intent_threshold` 1–10 (default 7), `copilot_followup_threshold` 1–50 (default 3), `copilot_exempt_stages[] {pipeline_id, stage_id}` (never flagged), `copilot_hot_lead_stages[] {pipeline_id, stage_id}` (hot flag also requires lead in one of these when non-empty) | W | Unknown keys silently dropped. Takes effect on next 30-minute compute tick |
| GET | `/copilot/flags` | Current Co-Pilot flags per lead | Read-only; confirms config is live | R | |

### User settings (extension / WAHA numbers and email identity)

| Method | Path | What it does | Key fields / constraints | R/W | Dangerous notes |
|---|---|---|---|---|---|
| GET | `/users/settings` | Read a user's AI/auto-responder switches and org email identity | Query `user_id` (admin only) or `phone_number` (resolve pipeline owner of that number); omit both for self. Returns `user_id, user_name, ai_replies_enabled, ai_stage_shifting, auto_create_leads, auto_responder_enabled, auto_responder_hours, auto_responder_auto_pause, auto_responder_pause_delay, bump_up_enabled, max_bump_up, af_personalization_enabled, from_name, reply_to, limit { is_limit_active, messages_limit, used_today, next_upgrade_at }, weekly_report_unsubscribed, notifications, organization { bump-up config }` | R | Extension/WAHA numbers have no account row; their switches live on the pipeline owner. Everything defaults to on on a paid onboarded account |
| POST | `/users/settings` | Update those switches (all `sometimes`) | `user_id` (admin only; non-admins get `403 "Access denied…"`); `ai_replies_enabled`, `ai_stage_shifting`, `auto_create_leads`, `auto_responder_enabled`, `auto_responder_hours` JSON **string** `{"start":"09:00","end":"20:00"}`, `auto_responder_auto_pause`, `auto_responder_pause_delay` JSON string `{"value":2,"unit":"hours"}` (≥1, max 7 days = 168 h / 10080 min), `bump_up_enabled`, `max_bump_up` 1–5, `af_personalization_enabled`, `from_name` ≤255 and `reply_to` email (**org-level**, required before email sequences), `is_limit_active` (cap itself set by Kraya), `weekly_report_unsubscribed` | W | **Turning `auto_responder_enabled` off cancels the user's pending scheduled messages.** Agent may configure existing members' settings but never adds members. `400 { message: "Bad Request", errors }` on validation |

### Round robin

| Method | Path | What it does | Key fields / constraints | R/W | Dangerous notes |
|---|---|---|---|---|---|
| GET | `/round-robin/settings` | Read round-robin config | Listed in the tool map (§22) only | R | Read before any `round_robin_assignment` rule |
| POST | `/round-robin/settings` | Update round-robin config | Admin-only (per role table) | W | Field shape not documented in this doc |

### Leads and conversations (read-only, for debugging)

| Method | Path | What it does | Key fields / constraints | R/W | Dangerous notes |
|---|---|---|---|---|---|
| GET | `/leads` | Find a lead | `page` and `count` **both required** (start `page=1&count=20`); `phone` (digits with country code, no `+`, fastest), `search` (name/email/phone), `id`, `chat_id` (`<digits>@c.us` or `<id>@lid`), `stage_id`, `pipeline_id`, `source`, `sort_by`, `sort` (default `leads.created_at` desc). Returns `id, name, phone, email, chat_id, source, stage_id, pipeline_id, attributes {key: value}, stage, pipeline { id, name, phone_number }, created_at, updated_at` | R | Agent never creates, edits or messages leads. Confirm the phone with ops when only a name is given; drop `pipeline_id` if nothing matches |
| GET | `/whatsapp/conversations` | Cloud API conversation list | `whatsapp_account_id` required; `query`, `page`, `count`, `has_unread`, `has_failed`, `has_needs_reply`, `filters` (JSON). Returns `id (lead id), name, phone, message_type, from_phone_number, content, last_message_at, is_read, open_session (24h window), unread_count, last_message_status` | R | |
| GET | `/whatsapp/conversations/messages` | Cloud API message history for a lead | `whatsapp_account_id` and `lead_id` required; `page`, `count`, `timestamp` (paginate backwards from unix). Newest first, reactions excluded; `from_phone_number` = business number ⇒ outbound; `status` (sent/delivered/read/failed), `error_code`, `wm_id`, `mode` | R | |
| GET | `/whatsapp/messages/{id}` | One Cloud API message with its media | — | R | |
| GET | `/waha/sessions` | Hosted WAHA sessions on pipelines the user can see | `{ success, sessions: [ { id, public_id, phone_number, display_phone_number, status, owner_name, owner_id } ] }`; `running`/`syncing` = connected; `starting`, `qr_ready`, `stopped`, `error`, `disconnected` = not connected | R | Nothing reads or sends when not connected |
| GET | `/waha/sessions/{public_id}/chats` | Chats on a WAHA session | `q`, `limit` ≤200, `offset`; returns `{ data: [ { id, name, lastMessage { id, timestamp, fromMe, body, hasMedia, type, ack, ackName } } ], has_more }` | R | Live read from the WAHA worker: a handful of calls per investigation, never poll; `premium_features` gate (403 on free) |
| GET | `/waha/sessions/{public_id}/chats/{chatId}/messages` | Messages in a WAHA chat | `limit` ≤100, `offset`, `downloadMedia`; `chatId` = `<digits>@c.us`, stored `@lid`, or `@g.us` for groups; returns `id, timestamp, fromMe, from, body, type, hasMedia, media { url, mimetype, filename }, participant`; `fromMe: true` = business side | R | Same: live, no polling; 500 "Something went wrong while fetching chat messages" = worker/session down, not empty |

Chrome-extension conversations are not stored server-side; the only record is the LangSmith trace `inputs.content` (`langsmith.md`). Complaint procedure: find lead → decide channel → pull last 20–50 messages → pull LangSmith runs → quote the specific messages and config line.

### Demo chat (public, by org slug; no credits, nothing persisted)

| Method | Path | What it does | Key fields / constraints | R/W | Dangerous notes |
|---|---|---|---|---|---|
| POST | `/demo/ai-chat/generate-reply` | One stateless reply from the org's live qualification prompt (as if lead were in New Lead) | `org_slug` required; `chat[]` ≥1 of `{ from: "other" (lead) \| "user" (bot), message, timestamp? }`, whole conversation oldest first. Returns `{ success, reply, should_send, send_files[] { file_id, caption, file_url, file_name, file_type }, change_stage { stage_id, stage_name } \| null }` | R | Rate limit 60/min per IP anonymous, 300/min with a paid org token (free-pack token: 15). Does **not** exercise attribute values, conversation summary, `flow_state`, `stage_history`, non-New-Lead stages, the sales-support template, or extraction jobs: single fact probes only; flow testing is the `ai-flow-testing` skill |
| POST | `/demo/ai-chat/check-qualification` | Is this chat qualified? | Same body; `{ success, qualified, reason, all_questions_answered }`; `403 "Demo qualification check is not enabled for this organization."` when `run_qualification_check` is off | R | |
| POST | `/demo/ai-chat/generate-bumpup` | Next bump-up text the org would send | `{ org_slug, chat[], index? }`; fixed step `index` in fixed mode, else AI-written; `"Max bump up message generation reached!"` past the last step | R | |
| GET | `/demo/ai-chat/organization-info` | Org demo settings | `?org_slug=…` → `organization.run_qualification_check` | R | |

### Product catalog

| Method | Path | What it does | Key fields / constraints | R/W | Dangerous notes |
|---|---|---|---|---|---|
| GET | `/organizations/catalog` | Read the org's one catalog | `{ catalog: null }` or `{ id, file_name, row_count, status pending→indexing→indexed\|failed, indexed_at, error_message, columns[] { name, suggested_type, distinct_count, blank_count, values / min, max }, filters[] { source_column, filter_key, type } }` | R | Poll every 15–30 s during indexing; compare `row_count` with the file (a parse failure can index zero rows and still say `indexed`) |
| POST | `/organizations/catalog` | Register an uploaded spreadsheet | `file_name` ≤255 with `.csv/.xls/.xlsx` (else `400 "Unsupported file type…"`); `url` from `/upload`. Reads within 60 s; `201` with status `pending`; `422 "We could not read that file…"`. Header row = column names, no required columns | W (admin) | **Deletes the org's existing catalog** (id changes). Nothing indexed until filters are mapped |
| POST | `/organizations/catalog/{id}/filters` | Map filter columns and start indexing | `filters[]` of `{ source_column (must exist in file), type enum\|number }`; replaces earlier mapping; `422 Column "X" is not part of the uploaded file`; `404` not this org's. `200 { message: "Mapping saved. Indexing your catalog now.", status: "indexing" }`; Kraya derives `filter_key` = `catalog_` + lowercased column | W (admin) | `filters: []` is accepted and useless (AI only gets filters when ≥1 exists). Expect 1–2 minutes of thin answers during re-index |
| DELETE | `/organizations/catalog/{id}` | Remove catalog and its vectors | `200 { message: "Catalog removed." }` | D (admin) | Needs `confirm delete` |

### Ops CRM (a different service: `https://ops.kraya-ai.com`)

| Method | Path | What it does | Key fields / constraints | R/W | Dangerous notes |
|---|---|---|---|---|---|
| POST | `https://ops.kraya-ai.com/api/hooks/agent` | Raise tasks and integrations on the client's Ops CRM card | Bearer `OPS_CRM_AGENT_TOKEN` (or a pasted `ops_…` token; CLAUDE.md names `AGENT_API_TOKEN`); exactly one of `orgId`, `krayaLeadId`, `clientId`; `source` (stored as author `agent: <source>`); `tasks[] { title, details, assignee? }`; `integrations[] { type (lowercase, board vocabulary: meta lead ad, whatsapp api, justdial, indiamart, zoho, google sheets, neodove, capi, …), assignee? }`. Idempotent on client + promise text / client + type; response `{ client { id, name, poc }, created { tasks[], integrations[] }, skipped {…} }` | W | Never closes anything. `404` = no card / no org id on card: report, do not retry. Reworded promises create duplicates: keep titles stable, read the card first when unsure. Without a token, list commitments in the handover instead |
| GET | `https://ops.kraya-ai.com/api/hooks/agent/clients` | Read the board (used by `ops-client-sweep`) | Same bearer; filters `stage`, `flag` (awol, iterations_requested, ongoing_issue), `poc`, `health` (Healthy, Monitor, At Risk, Critical, Churned), `name`, `sale_from/to`, `handed_over_from/to` (`YYYY-MM-DD`), `limit` ≤500 (default 100), `offset`. Returns `clients[] { id, name, phone, chatId, orgId, krayaLeadId, stage, stageSince, flags[], poc, healthRisk, healthScore, packSold, saleDate, handedOverAt, activatedAt, reminderAt, credits, creditsUsed, nextPaymentAt }, count, hasMore, filters`; deleted cards never returned; 400 names a bad date, 401 wrong token | R | Reaches the Ops CRM, never a client's Kraya account |

### Mentioned but explicitly out of scope (no agent power)

`POST /auth/signup`, SuperAdmin one-time login links / org search / plan changes, `/organizations/invite` and signup with `organization_id` (adding team members), `POST /pipelines`, message queue priority, website widget, `POST /leads` / imports / sending messages to leads, integrations, connecting WhatsApp API or Instagram accounts, billing, and the `interactive_options_enabled` prompt flag (no org-admin or SuperAdmin route reads or writes it, `/users/metadata` does not expose it; a developer must set it).

## 3. Important behavioural notes stated in the doc

**Upsert semantics and ids**
- Upserts key on `id` being present, not non-null (`$request->has('id')` is true for `null`); never send `"id": null`.
- Custom attributes are keyed by `key` (string), never by id; rename via `new_key_name`. Rules update via `rule_id`, not `id`. `set_lead_attribute` and `from_attribute` scheduling take the numeric attribute **id**, not the key.
- Ids to read from the account, never guess: pipeline ids, every seeded stage id by slug (from `/users/metadata`), attribute ids/keys, sequence ids (`data.id`), rule ids (`data.id`), message ids, template ids, and above all `whatsapp_account_id` (from `GET /whatsapp/accounts`, `status: "active"` only).

**Sequences**
- `enabled` on a sequence is a dead column: returned and accepted, nothing reads or writes it (the upsert never writes it, no route sets it, nothing that sends reads it, in Laravel, the extension or the dashboard). Never read it, never report it, never send it, never infer inactivity from it. Rules start sequences through `activeSequence()`, which excludes only deleted sequences. What actually stops follow-ups: the sequence being deleted, the assigning rules removed or disabled (`rules.enabled` is a real gate), a `stop_assigned_sequence` rule, or `auto_responder_enabled` off on the WhatsApp API account (§13) or the user (§20). Verified against `AutoResponderController@upsertSequence` and `RuleService`, 2026-09-15.
- Messages are upserted one at a time by `id`; omitted messages are kept, never deleted; `order` is not renumbered (send every remaining message's `id` with its intended `order`); re-sending a message without its `id` fails with 409 and rolls back the whole update; remove messages only with `DELETE /auto-responder/sequence-message`; read back with `GET /auto-responder/sequences?sequence_id=` after every change.
- `mode` and `whatsapp_account_id` are set only on create; step types are checked against the stored mode. To change channel or number, duplicate the sequence and reassign rules.
- Sequence names unique per org (builder retries with `" (Agent Hub)"`, `" (Agent Hub 2)"` … up to 5). Message names, subjects and near-identical content unique within a sequence.
- `whatsapp` messages only on `extension` sequences; `whatsapp_template` only on `whatsapp_api` sequences with an APPROVED template on the same number. `ai_call` needs an active AI calling agent configured by Kraya (silently skipped otherwise) and cannot use `recurring`. `reminder` sends nothing to the lead.
- Template variables resolve at send time: custom attribute with that exact key, then built-ins `lead_name`, `lead_first_name`, `name`, `email`, `org_name`, `user_name`, `booked_slot`, then the template's example value. Name variables after real attribute keys or built-ins.
- `message_delay` accepts only `schedule` plus its matching sub-object; agents translate `{value, unit}` (0 → immediate); delay is relative to the previous message (sequence start for message 1), or to the booked slot on calendar reminder sequences.
- Content conventions: `{lead_first_name}` only (never `{lead_name}`), `*bold*`, `-` bullets, short paragraphs, no em/en dashes, emoji only on request, one `Reply *KEYWORD*` CTA per message, `Reply *STOP* to unsubscribe.` last line on marketing nurture, 4–6 messages per sequence, no ops notes / `[Insert …]` / `TBD` / placeholder testimonials.

**Stages**
- Kraya seeds six inbox views (`All Chats`, `Unread Chats`, `Needs Reply`, `Groups`, `Pending Reminders`, `Queue`; `is_default: true`, orders 0–5): leave them alone. Funnel stages (`New Lead`, `Qualified`, `Nurturing`, `Good Lead`, `Lead Won`, `No Response`, `Deleted` at order 100; generic description, `ai_switch: true`, colour `#000000`) may be edited, described, recoloured, reordered, `ai_switch`-toggled or deleted.
- **New Lead and Qualified must never be renamed**: Laravel matches them by name and slug to choose the qualification prompt, auto-move a qualified lead, and file new leads.
- Every stage must carry a one-sentence `description` (the AI reads it when deciding moves).
- New custom stages must have `order` strictly greater than Qualified's real `order` (7 on a fresh account; some pipelines have 6 or 7; read it, do not hardcode 3).
- `ai_switch: false` silences the AI in that stage regardless of account or user switches; set it off on Hot Lead, Human Intervention, negotiation, Lead Won, Deleted and ops-tracking stages.
- `DELETE /stages/{id}` refuses default stages and stages holding leads.

**Attributes**
- Keys match `^[a-zA-Z0-9 _./&-]+$`, ≤150, Title Case with spaces (no `+`, parentheses). Dropdown/multi_select `values` max 50 (extras silently dropped). Changing `data_type` or `values` starts a background cleanup: poll `sync-status` until `idle`. Undescribed attributes extract badly. Build 6–12 attributes; workflow-state names/values must match the qualification spec exactly.

**Org info**
- `POST /organizations/info` is a full replace; `bot_languages` and `about` are blanked when absent; `attachments` and `sendable_files` must be JSON strings; saving re-indexes asynchronously. `qualification_requirements` has up to eight `##` sections whose attribute names, values and stage names must match the real ones exactly.

**Rules**
- `trigger_conditions[]` (OR'd) and `attribute_conditions[]` (AND'd) replace on update. Send only the chosen action's fields; stray ones are rejected by name. Identical rules are rejected (`identical_rule`): treat as skip. `round_robin_assignment` not allowed with `no_response_from_lead`. `send_template_message` / `schedule_message` templates must be APPROVED on the named account with example values for every variable. `schedule_message` `text_api` only delivers inside the 24h window; use `template` outside it. `from_attribute` scheduling needs a `datetime` attribute. All matching rules fire in `processing_order`.
- Core rule set for a new account: STOP keywords → stop sequence + second rule toggle AI off; Won/Lost → stop sequence; silence (24/48/72h) in New Lead + Qualified → No Response; No Response → re-engagement sequence; sequence completed → dormant stage; Human Intervention → AI off + stop sequence; stage → its sequence for every pair. Consolidate conditions into one rule across stages and pipelines; never create intent-keyword → Qualified rules (qualification is the AI's job). With a WhatsApp API account, silence routing and reminders can use templates (work outside the 24h window).

**WhatsApp API and templates**
- Templates cannot exist without a connected account; `has_whatsapp_business_account` is the cheap pre-check. Kraya does not de-duplicate `display_name` (creates `"<name> v2"`). Approval is asynchronous via Meta; only APPROVED templates can be wired into sequences, rules, new-lead broadcast or contact-info settings, so create and submit templates first, then wait. Deleting a template detaches it and soft-deletes any sequence left empty. `whatsapp_account_id` in template listings is a string.
- Sendable templates: Cloud API only; never more than one per turn; the runtime tells the model to leave `message` empty when it sends one.

**Switch precedence**
- A WhatsApp API number uses its account row (`POST /whatsapp/accounts`); an extension/WAHA number uses its pipeline owner's `/users/settings`; a stage with `ai_switch: false` silences the AI regardless. Turning `auto_responder_enabled` off on a user cancels pending scheduled messages. `from_name` / `reply_to` are org-level and required before any email sequence goes out.

**Bump-ups, calendar, Co-Pilot, catalog**
- Bump-up content is set org-wide via `POST /organizations/bumpups`; whether and how many fire comes from `bump_up_enabled` / `max_bump_up` (max 5) per account or user. Steps 1–5, delay 1–10080 minutes.
- Calendar: 403 on free orgs; one reminder sequence per calendar (409 otherwise); reminder delays anchor to the booked slot; `call_slot_booked` is the rule trigger for confirmations and pre-visit reminders.
- Co-Pilot: only five writable keys; unknown keys dropped; changes apply on the next 30-minute tick; `GET /copilot/flags` confirms.
- Catalog: one per org, a new upload deletes the old one; no progress callback (poll 15–30 s); `filters: []` indexes rows the AI never narrows; at reply time the AI runs a filter-generation step and retrieves `top_k` catalog rows (fallback: all rows) alongside `top_k` knowledge-base rows; works with every reply template. Verified 2026-09-28.

**Interactive options (§25)**
- Tappable buttons/lists need `interactive_options_enabled` on the org's prompt config, a Cloud API number, and a qualification prompt (New Lead). Limits: 1–3 options without descriptions → reply buttons (title ≤20, body ≤1024); up to 10 or any with description → list (row title ≤24, description ≤72, `options_label` ≤20, body ≤4096); more than 10 → plain text. No API route can turn it on; no test path renders them (demo chat, flow-testing harness, SuperAdmin simulations, hosted sessions); verify only through LangSmith traces of real Cloud API replies. Verified 2026-10-06.

**Rate / credit / load considerations**
- Demo chat: no credits, nothing persisted; 60 calls/min per IP anonymously, 300/min with a paid org token, 15/min with a free-pack token.
- WAHA chat/message reads hit the live session store: a handful of calls per investigation, never poll.
- Catalog registration reads the file within a 60 s limit; indexing retries 5 times before `failed`.
- One mutation per turn in conversational mode (batched large upserts intermittently produce malformed tool calls).
- The `ai-flow-testing` skill (not this doc) spends real credits, within a 1,000-credit budget per CLAUDE.md.

**Operating protocol (§21, adopt verbatim)**
- Disambiguate → discover (list endpoint, real ids, never memory) → propose (`Current:` / `Proposed:` / `Proceed?`, wait for explicit yes, then the single mutation with no re-explaining) → mutate one entity per turn, reporting `1/N done — <name>`.
- Deletes: quote the entity and what is lost, ask "Are you sure?", then require the literal `confirm delete`; "wait" / "stop" / "cancel" aborts.
- Edits merge, never overwrite. Distinguish "edit the day-2 message" from "replace the sequence" (delete old messages, then create new) and ask if unsure.
- For rules, fetch sequences, stages and pipelines first so cross-reference ids are real. Templates: `GET /whatsapp/accounts` first, reconcile names with `count=1000`, tell the rep submission means PENDING.
- Keep a checklist for anything over ~3 steps so a trimmed context cannot cause double work.

## 4. What the doc says the agent must NOT do

- Never sign up accounts, change the pack/plan, add team members (`/organizations/invite`, signup with `organization_id`), connect a WhatsApp API or Instagram account, change phone numbers, touch billing, or anything needing Kraya admin / SuperAdmin permissions. Say so and point the rep to the dashboard.
- Never use `POST /pipelines` (multi-pipeline setup is ops work in the UI), message queue priority, the website widget, lead ingestion, imports or integrations.
- Never create, edit, or message leads (`POST /leads` and message sending are out of scope); lead and conversation endpoints are read-only lookups.
- Never send `"id": null` on an upsert.
- Never guess a `whatsapp_account_id` or any other id; never use an account whose `status` is not `active`.
- Never rename the New Lead or Qualified stages; never touch the six seeded inbox views; never hardcode the Qualified order.
- Never read, report, send, or infer anything from a sequence's `enabled` column, and never report a sequence as inactive because of it.
- Never send a sequence update expecting omitted messages to be deleted, and never re-send an existing message without its `id`.
- Never omit `org_name`, `about` or `bot_languages` on `POST /organizations/info`; never send `attachments` as an array.
- Never send fields that do not belong to the chosen rule action; never create intent-keyword → Qualified rules.
- Never create a template without first reconciling names via `GET /whatsapp/templates?count=1000`; never wire an unapproved template or a template without example values for every variable.
- Never map an empty catalog filter list and expect narrowing; never write the catalog without the org info carrying the catalogue rules.
- Never invent prices, testimonials, numbers, phone numbers or URLs; never ship ops notes, `[Insert …]`, `TBD`, placeholder testimonials, em/en dashes, or unrequested emoji in lead-facing content; never use `{lead_name}` in sequences.
- Never mutate without discover → propose → explicit confirmation; never more than one mutation per turn; never delete without the literal `confirm delete`.
- Never poll the WAHA chat endpoints; never paste whole conversation histories to the rep; never include another lead's messages.
- Never retry a `404` from the Ops CRM write-back (report it); never reword a tracked commitment into a duplicate task; never expect the Ops CRM endpoint to close tasks.
- Never try to switch `interactive_options_enabled` over the API (there is no route); ask a developer and note it in the handover.
- Never write credentials or tokens to files, logs, commits or replies (CLAUDE.md), and never modify the api-reference field tables from memory.