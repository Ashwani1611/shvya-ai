# Reading production AI traces in LangSmith

Every AI reply Kraya's LLM service generates (qualification replies, bump-ups, summaries, call analyses) is traced to LangSmith. When a rep says "the bot said something wrong to this lead", "it switched to Hindi", "it moved the lead to the wrong stage", or "it never sent the brochure", the trace shows the exact rendered prompt (org info, qualification spec, retrieved FAQs and knowledge-base chunks, stage list, sendable files, conversation) and the structured output the model returned. Read the trace before changing the account; most "AI bugs" are configuration the trace makes visible.

## Access

| Item | Value |
|---|---|
| Base URL | `https://api.smith.langchain.com` |
| Auth header | `x-api-key: $LANGSMITH_API_KEY` (from the environment; never print or store it) |
| Project | **`default`** is production. Query it by its session id `953ee94f-d9ce-4a9a-b5d9-6bc1ef148675` (`"session": ["953ee94f-…"]`). Do not use `kraya-staging`; it is empty |
| Retention | **14 days**. Anything older is gone; say so instead of searching further |
| UI link for a run | `https://smith.langchain.com/o/08c588ae-fe0c-5c4a-96d6-22d113e20b71/projects/p/953ee94f-d9ce-4a9a-b5d9-6bc1ef148675/r/<run_id>` (paste this in the handover so the rep can open the trace) |
| Rate limit | roughly 10 rapid calls before a `429`; space calls a few seconds apart, and page with `limit` ≤ 100 |
| Transport | plain `curl` with the body in a file (`-d @body.json`); some Python HTTP stacks fail TLS on ops machines |

## What is on every root run

Root runs carry `extra.metadata` set by the LLM service:

| Key | Meaning |
|---|---|
| `organization_id` | Kraya org UUID (`user.organization.id` from `/users/metadata`). Filter on this for "everything for this client" |
| `lead_id` | Kraya lead id (integer). Filter on this for "what happened with this lead" |
| `request_mode` | the prompt template used: `qualification-v4`, `qualification-v3.3`, `sales-support-all-stages-v2`, `bump-up`, `internal-conversation-summary`, `qualification-summary`, `qualification-demo` (the public demo chat), … |
| `environment` | `prod` on 99.9% of production runs; use it to exclude stray staging traffic |
| `namespace` | the org's Pinecone knowledge-base namespace |

A few batch jobs use `org_id` and `lead_ids[]` instead (ops conversation analysis, broadcasts). Common root run names: `Completion Chain` (the standard AI reply), `Balanced Boat Completion Chain`, `Starlite Completion Chain`, `EStore Completion Chain` (org-specific variants), `Chat`, `Message Variation`, `Optimize Message`, `Generate Qualification Questions`, `AI Call Outcome`, `Fireflies Call Analysis`, `Ops Conversation Analysis`. Filter by metadata first and by name only to narrow.

Where the lead id comes from: `GET /leads?page=1&count=20&phone=<digits>` or `&search=<name>` on the Kraya API (`api-reference.md` §19), the lead's dashboard URL, or the trace itself by filtering on the org and a time window and reading `lead_id` off the matching runs.

## Queries

All queries are `POST /api/v1/runs/query` with a JSON body. The filter language is LangSmith's: `eq`, `neq`, `gt`, `lt`, `has`, `and`, `or`, `search`.

Everything for one org in a window (newest first):

```json
{
  "session": ["953ee94f-d9ce-4a9a-b5d9-6bc1ef148675"],
  "is_root": true,
  "filter": "and(eq(metadata_key, \"organization_id\"), eq(metadata_value, \"<org-uuid>\"))",
  "start_time": "2026-09-03T00:00:00Z",
  "end_time": "2026-09-04T00:00:00Z",
  "limit": 100,
  "select": ["id", "name", "start_time", "status", "error", "extra", "outputs", "latency", "total_tokens"]
}
```

One lead's conversation history:

```json
{
  "session": ["953ee94f-d9ce-4a9a-b5d9-6bc1ef148675"],
  "is_root": true,
  "filter": "and(eq(metadata_key, \"lead_id\"), eq(metadata_value, \"<lead-id>\"))",
  "limit": 100,
  "select": ["id", "name", "start_time", "inputs", "outputs", "extra"]
}
```

Only qualification replies for an org, and only failures:

```
and(eq(metadata_key, "organization_id"), eq(metadata_value, "<org-uuid>"), eq(metadata_key, "request_mode"), eq(metadata_value, "qualification-v4"))
and(eq(metadata_key, "organization_id"), eq(metadata_value, "<org-uuid>"), eq(status, "error"))
```

`lead_id` is stored as an integer in the metadata; both `eq(metadata_value, "5315432")` and `eq(metadata_value, 5315432)` match it (verified on production). The org id is a string and needs quotes.

Pagination: the response is `{ "runs": [...], "cursors": { "next": "lt(cursor, '…')", "prev": null } }`; pass the `next` value unchanged as `"cursor"` in the following body. Sort with `"order": "asc"` when you want a conversation in chronological order. These filters were verified against the production project on 2026-09-04.

The children of one trace (retriever calls, the LLM call, tool calls):

```json
{ "session": ["953ee94f-…"], "trace": "<trace_id of the root run>", "limit": 50, "select": ["id", "name", "run_type", "inputs", "outputs", "start_time"] }
```

One run in full: `GET /api/v1/runs/<run_id>`.

## Reading a reply trace

- **`inputs`** on the root run hold the rendered request: `org_info` (`about`, `qualification_requirements`, `bot_languages`, `sendable_files`), `content` (the conversation as the model saw it), `lead_data` (stage, attributes), `stages_information`, and the retrieved `faqs` / knowledge-base `context`. If the spec you wrote is not there verbatim, the account was not saved the way you think.
- **`outputs.output`** is a JSON string; parse it. For qualification replies it contains `message`, `should_send`, `change_stage` (`{stage_id, stage_name}` or null), `send_files`, `stage_change_evaluation`, `chain_of_thought`, and `options` for interactive lists. `stage_change_evaluation` is the model's own reasoning about the stage move and is the first thing to read when a lead was routed wrongly.
- **Children named `Vectorstore Query` / `Vectorstore Search with Filter` / `FAQ Rerank`** show what the knowledge base returned. An empty result with a question the FAQs should answer means the FAQ was never indexed or is phrased too differently from how leads ask.
- **`status: "error"` with `error`** text is a service failure, not a config problem; report it to engineering with the run id.
- **`latency`, `total_tokens`, `total_cost`** are on the root run when selected. Cost aggregation across many runs via `POST /api/v1/runs/stats` works only without metadata or tag filters and without `end_time`; with those it returns zero. Count runs instead of summing cost when you filter by org.

## Typical investigations

| Rep says | Do |
|---|---|
| "It made up a number / date / link / venue / name" | find the value in `outputs.output`, then search `inputs.org_info.qualification_requirements` and the FAQs for an instruction that asked for that category without supplying it (a promise without material), then for a near-identical real value it anchored on; follow `conflict-audit.md` §5 |
| "The bot answered in Hindi to an English lead" | pull the lead's runs, check `inputs.org_info.bot_languages` and any language rule inside `qualification_requirements`; the fix is the org info, not the prompt |
| "It asked the same question twice" | read consecutive replies' `inputs.content` and `flow_state`; check the spec's never-re-ask rules and whether the question maps to a named attribute |
| "It moved the lead to the wrong stage" | read `stage_change_evaluation` and `stages_information` in the inputs; usually a stage without a description or a Stage Shifting row that names a stage that does not exist |
| "It quoted a price we never gave it" | search the retrieved FAQ / KB chunks in the children; the number is either in an FAQ, in `about`, or in a scraped attachment |
| "It never sent the brochure" | check `inputs.sendable_files` (is the file there, with a trigger in its description?) and `outputs.send_files` |
| "Nothing replied at all" | filter `status: "error"` for the org and window; if no runs exist for the lead at all, the message never reached the LLM (AI off for the stage or user, credits exhausted, number not connected): check the account, not LangSmith |

Never paste an entire trace into the conversation with the rep. Quote the specific input field or output line that explains the behaviour, give the run's UI link, and state the configuration change you propose.
