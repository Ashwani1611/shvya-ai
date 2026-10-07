# Dialnexa API and Kraya-side mechanics

Base URL `https://api.dialnexa.com/v1`, header `Authorization: Bearer <key>`. Keys are per workspace; the workspace is resolved from the key, no org header. All responses are `{"statusCode":200,"data":...}` except `/calls` which returns a bare list.

## Agents

| Call | Notes |
|---|---|
| `GET /agents` | All agents in the workspace with every version inline (`versions[]`, newest first) |
| `GET /agents/{id}` | One agent. Top level: `current_version_number` (the draft), `timezone`, `pipeline_type` (`Cascaded` = STT→LLM→TTS, `Speech_To_Speech`). Each version: `is_published`, `prompt_text`, `system_prompt_text` (Dialnexa's platform prompt, read-only), `llm_id`, `llm_temperature`, `voice_id`, `voice_speed`, `language_id`, `welcome_message`, `boosted_keywords`, `max_call_duration_sec`, `fallback_llm_enabled`, `postcall_analysis[]`, turn-taking knobs |
| `PATCH /agents/{id}` body `{"version_number": N, ...fields}` | Edits version N in place. `version_number` is required. Unknown fields are silently ignored: re-read to verify. Accepts `prompt_text`, `welcome_message`, `llm_id`, `llm_temperature`, `voice_speed`, `max_call_duration_sec`, `fallback_llm_enabled`, `boosted_keywords`, `postcall_analysis`, `title`, `timezone` (agent-level, still needs a version_number) |
| `PATCH /agents/{id}` body `{"version_number": N, "is_published": true}` | **This is publish.** Live = highest published version. Dialnexa opens N+1 as the next draft. No `/publish` endpoint exists |
| `GET /llms`, `/voices`, `/languages`, `/transcribers` | ID → name maps. Known: `llm_9208cb4d8e6203` GPT-5.4 Nano, `llm_739b4d91c2927e` GPT-5.4 Mini, `llm_meqtu4wm4xh2c6` GPT-4o Mini, `llm_meqtu4wbtl6886` GPT-4.1, `lang_IPH82yvF7eN9oJ` Hinglish, `lang_mfwedjx8dmjgdc` Multilingual |

`boosted_keywords`: string, comma separated, letters/digits/spaces only (Devanagari → 400), max 100 terms.

`postcall_analysis` entry shape:
```json
{"field_name":"Outcome","field_description":"The single outcome category reached on this call.","field_type":"SELECTOR","additional_fields":["Appointment Payment","Qualified","Disqualified"],"display_order":0}
```
`field_type` ∈ SELECTOR | BOOLEAN | TEXT. Results come back in the webhook as `$.payload.call.post_call_analysis` and in `GET /calls/{id}` as `postcallanalysis`.

## Calls

| Call | Notes |
|---|---|
| `GET /calls?limit=N` | Bare list: `id, agent_id, to_number, called_time, duration, status (completed/did_not_pick/busy/initiated), end_reason, llm_name, voice_model_name, postcallanalysis`. **No transcript** |
| `GET /calls/{id}` | Same plus `recording_sas_url` (Azure blob, expires in ~7 days). Still no transcript |
| `POST /calls` `{agent_id, phone_number:"+91…", metadata:{...}}` | What `DialNexaService::outboundCall` sends. `metadata` keys become `{{dynamic_variables}}`; Kraya sends `lead_data` (formatted block) plus every custom attribute, `call_instructions`, `call_type` |

Transcripts live only in the webhook. Kraya stores every webhook body in `dialnexa_webhook_payloads.payload` (JSON): `$.payload.call.{id,status,transcript,summary,recording_url,post_call_analysis,hangup_reason,duration_in_seconds}`. Transcript is a JSON string of `[{role, content, start, end}]`. Assistant lines are STT of the TTS audio. Query:
```sql
SELECT JSON_UNQUOTE(JSON_EXTRACT(payload,'$.payload.call.transcript')) FROM dialnexa_webhook_payloads
WHERE payload LIKE '%call_xxx%' AND JSON_UNQUOTE(JSON_EXTRACT(payload,'$.payload.call.status'))='completed';
```
Dashboard test dials also hit the webhook (agent-level webhook), so they appear here but have no `lead_calls` row.

Recording is stereo: left = caller, right = agent. Detect dropped caller speech:
```bash
ffmpeg -i call.mp3 -af "pan=mono|c0=FL,silencedetect=noise=-35dB:d=0.4" -f null - 2>&1 | grep silence_
```

## Kraya side

- Agent row: `ai_calling_agents` (one per org): `agent_id`, `provider='dialnexa'`, `dialnexa_org_id`, `api_key` (KMS-encrypted; `AiCallingAgent->api_key` accessor decrypts; in tinker: `(new App\Services\KMSService())->decrypt($cipher)`), `webhook_secret`.
- Outbound: `SendAutoResponderAiCall` → `AiCallSequenceService::resolveCallInstructions()` (step `content` → `call_instructions`, `call_type=follow_up`) → `DialNexaService::outboundCall()`. Lead payload: `Lead::buildAiCallLeadData()` = `lead_first_name, lead_name, phone, email, org_name, booked_at` + every `lead_custom_attributes` key + `lead_data` (the same as "key: value" lines). No `current_time` is sent; the agent timezone must be Asia/Kolkata.
- Inbound: `WebhookController` → `ProcessDialNexaWebhookPayload` → `DialNexaService::handleCallEnded()` → `lead_calls` (`agent_call_id` = Dialnexa call id, `call_notes` = transcript + summary), `leads.notes` summary, then the outcome-analysis job writes custom attributes and moves the stage.
- Update the step: `POST /api/auto-responder/sequence` with `{id, mode, messages:[{id, message_name, message_type:"ai_call", content, message_delay, order, call_retry_limit, call_retry_interval, call_retry_interval_unit, call_outcome_instructions}]}`; read the current payload from `GET /api/auto-responder/sequences` first and only change `content`. Only messages in the payload are touched. Bearer tokens are revoked on re-login (401 on every route, exp still valid).
- Preview without a lead: `POST /api/auto-responder/sequence-message/preview-call`.
