# Reference contents

- Bounded review of real behavior
- Choose and state the cohort
- Probes and attribution
- Evidence contract for a finding
- Complete review method from supplied reference
- The production-traffic sweep
- 1. Pull the org's runs
- 2. Split real traffic from noise
- 3. Read the live config out of a run
- large runs return empty inputs; the payload is behind a presigned URL, fetched without the key
- 4. The probes
- 5. Before any of it becomes a finding

# Bounded review of real behavior

Current native tools provide persisted messaging and sanitized diagnostics, not a general raw model-trace population API. Do not carry over a legacy LangSmith project ID, retention assumption, SQL table or credential lookup.

## Choose and state the cohort

Start with the user's concrete incident identifiers. Resolve a person/phone/email through `find_leads` and inspect ambiguity before choosing a lead. Use `find_affected_leads` for a specific persisted issue signal (qualification completion mismatch, Workflow/AI/delivery failure or stalled stage). Combine with `get_conversion_analysis` and `get_runtime_health` for returned aggregate context.

Record selection method, limits, period, timezone and returned count. Deduplicate lead IDs across issue cohorts. This is an incident-focused sample, not a representative random sample. Where tools do not offer pagination, do not invent cursors or repeatedly widen queries and claim exhaustiveness. An org-wide rate needs a genuine denominator from compatible aggregate evidence.

For each selected lead use `get_lead_snapshot`, `get_conversation` and the relevant message/Workflow/qualification diagnostic. Store source IDs and tool read timestamps. A conversation is recent and bounded; it is not guaranteed lifetime history. Distinguish message content, generated output, enqueue success, provider acceptance and confirmed delivery/read when those states are available.

## Probes and attribution

- Links: compare exact sent strings to approved current and incident-time references; do not manufacture slugs or infer delivery from a clickable-looking URL.
- Commitments: distinguish asking a preferred time from confirming a booked slot, callback or named staff availability. Seek the actual scheduling/dispatch result before treating the commitment as fulfilled.
- Figures and policies: search About, full available Playbook, FAQs, Cadences and Touchpoints for the exact number/claim and context. If found, identify the conflicting source. If absent, do not conclude hallucination while document content or historical runtime prompt is unavailable; report an unsupported claim with attribution unresolved.
- Unsupported offering: compare the precise affirmative reply to the approved business scope and exclusions. A question about a service is not confirmation it is offered.
- Repetition: distinguish duplicate delivery of the same message, duplicate generation, legitimate reminder and a human reply. Normalize conservatively; preserve message IDs and timing.
- Handoff/opt-out: trace whether continuing messages came from AI auto-reply, a Cadence, Workflow or human operator. A correct qualification decision can coexist with a follow-up path that never stopped.
- Human/AI overlap: use explicit available sender/automation metadata. Missing from a model-output set does not prove a human wrote a message; attribution may be unavailable.
- Timing: use offset-aware timestamps and comparable definitions. Pair inbound and first eligible outbound only when both are visible; identify bot versus all-response latency. Split business/outside hours only with known schedule/timezone. Bounded recent conversations can censor the next reply, so report those cases separately.
- Errors: count attempts and distinct leads separately, then check later success or delivery for each affected lead. A retry storm that recovered differs from unreplied customers. Do not assume all errors are harmless; do not infer wasted spend without cost evidence.

## Evidence contract for a finding

Record ID, requirement, exact source quotation/message ID, observed period, affected distinct leads in sample, compatible denominator if known, current configuration, incident-time uncertainty, attribution layer/confidence, impact and owner. Native counters, simulations and message traces each prove only their documented scope. If raw call recordings, historical prompts, document text or provider logs are needed but absent, say exactly which question remains unanswered.


# Complete review method from supplied reference

> SHVYA ADAPTATION: Read runtime-contract.md before using this full reference. Preserve this method's detail, but compile its output into native SHVYA schemas. Kraya field names, API routes, database queries, token scripts and past performance claims are historical context, not live capabilities. Sample businesses, prices and policies remain examples. Human handoff/opt-out takes precedence over continued qualification; use verified double-brace CRM tokens and provider bindings. This reference does not authorize sending, enrollment or activation.

# The production-traffic sweep

`kraya-account-setup/references/langsmith.md` covers access, the run shape and investigating a single lead. This file is the population sweep across an entire org, which is what finds defects no configuration read can see.

Keep this in the main thread. The judgement calls — is this a hallucination or a config entry, is this an outage or a retry storm — are exactly the ones that go wrong when delegated.

## 1. Pull the org's runs

Daily windows, not hourly: one org is low enough volume that pagination handles it, and 24× fewer requests means no rate limiting. Cache per window so a re-run is free.

```python
"""Root runs for one org -> roots.jsonl."""
import datetime, json, os, re, subprocess, time

KEY = re.search(r"lsv2_pt_[a-f0-9_]+", open('<creds file>').read()).group(0)
SESSION = "<langsmith project id>"          # prod project; see langsmith.md
ORG = "<organization_id>"
FILTER = f'has(metadata, \'{{"organization_id": "{ORG}"}}\')'

def q(body):
    """POST runs/query with backoff. 429 returns an HTML page, not JSON."""
    for attempt in range(8):
        p = subprocess.run(["curl", "-s", "-m", "120", "-X", "POST",
                            "https://api.smith.langchain.com/api/v1/runs/query",
                            "-H", f"x-api-key: {KEY}", "-H", "Content-Type: application/json",
                            "-d", json.dumps(body)], capture_output=True, text=True)
        try:
            d = json.loads(p.stdout)
            if 'runs' in d:
                return d
        except Exception:
            pass
        time.sleep(5 * (attempt + 1))
    raise RuntimeError(p.stdout[:300])

def window(w):
    fn = 'win/' + w[0][:10] + '.json'
    if os.path.exists(fn):
        return json.load(open(fn))
    out, cursor = [], None
    while True:
        body = {"session": [SESSION], "is_root": True, "start_time": w[0], "end_time": w[1],
                "limit": 100, "filter": FILTER,
                "select": ["id", "trace_id", "name", "start_time", "end_time", "extra",
                           "outputs", "error", "status"]}
        if cursor:
            body["cursor"] = cursor
        d = q(body)
        out.extend(d['runs'])
        cursor = (d.get('cursors') or {}).get('next')
        if not cursor or not d['runs']:
            break
    json.dump(out, open(fn, 'w'))
    return out
```

Window the range from a little before the account went live to now. Note the 14-day trace retention: on an older account the early weeks are simply gone, and that is a limit to state, not a finding.

## 2. Split real traffic from noise

Three filters, all mandatory:

- `metadata.template` — the qualification template (`qualification-v4` and similar). Summary and variation runs are separate templates and are not replies.
- `metadata.request_mode` — **drop `qualification-demo`.** That is playground and ops testing, not leads. Its volume is itself a signal: three demo runs means nobody tested; a hundred and thirty means ops iterated hard, which usually corroborates an `iterations_requested` flag.
- `metadata.environment == "prod"`, to keep replays and local runs out.

Then aggregate by **distinct `lead_id`**, never by run. Report: real replies, distinct leads, replies-per-lead distribution, stage-change decisions, `should_send` split, and how many replies attached files or offered options.

The replies-per-lead distribution tells you where conversations die. A large single-reply bucket means leads open and never continue.

## 3. Read the live config out of a run

One fetched run gives you the rendered `org_info`, the stage list, the model and the transcript — the account's true runtime state, including when you have no database.

```python
d = json.loads(subprocess.run(["curl", "-s", "-m", "60",
      f"https://api.smith.langchain.com/api/v1/runs/{run_id}",
      "-H", f"x-api-key: {KEY}"], capture_output=True, text=True).stdout)
# large runs return empty inputs; the payload is behind a presigned URL, fetched without the key
if not d.get('inputs') and (d.get('inputs_s3_urls') or {}).get('ROOT', {}).get('presigned_url'):
    d['inputs'] = json.loads(subprocess.run(["curl", "-s", "-m", "60",
        d['inputs_s3_urls']['ROOT']['presigned_url']], capture_output=True, text=True).stdout)
p = d['inputs']['params']   # org_info, stages_information, content, lead_data, model, ...
```

`params.content` is the conversation as the model saw it. `params.lead_data.attributes` are the attributes it could **read** — one turn stale, because they are written afterwards by the summary job.

For transcripts, fetch the **last** run per lead: its `content` holds the whole conversation. Sample deliberately — every lead the bot routed to an interesting stage, the longest threads, and a random tail — rather than fetching all of them.

## 4. The probes

Run these over the collected `outputs.output.message` strings.

**Links.** Extract every URL and count by exact string. Compare against the account's real slugs from `calendar_configurations` or the org info. Variants differing by a hyphen or a missing word are corrupted regenerations, and each one is a dead link a lead clicked.

**Commitments.** Regex for the bot confirming a time or a callback (`we'll call you at`, `will reach out at`, `tomorrow at`). Distinguish **asking** for a time, which is fine, from **confirming** one, which commits a human who never agreed. Report only the confirmations, with the count of distinct leads.

**Withheld figures.** Regex for currency and bare amounts. **Then grep the org info, FAQs and sequence copy for each figure found.** In the config means the finding is a policy deviation for ops to resolve with the client; absent from the config means the model invented it. These are different reports with different owners, and getting it backwards wastes a day.

**Out-of-catalogue confirmations.** Take the exclusions from the `about` block and search for the bot affirming them.

**Duplicates.** Group messages by lead and count identical normalised strings.

**Human versus bot on a shared channel.** Build the set of strings the model actually produced, then walk the transcripts: any outbound line not in that set was typed by a human. Terse trade shorthand, contact details the prompt forbids, and image bursts are the giveaways — the bot cannot send images when `sendable_files` is empty. Count the conversations where both replied.

**Latency.** Parse the transcript timestamps, take each lead message and the next outbound, and report median, 75th and 90th percentiles, plus the same split for leads who wrote outside the configured hours. Bot timestamps come from the transcript, so this works without any database.

**Errors.** Group failures by distinct lead and read the error text. Then ask, per affected lead, whether a later run succeeded. A spike that resolves to a handful of leads that all recovered is a retry storm: worth a line about wasted spend, not a client-impact finding.

## 5. Before any of it becomes a finding

- Count distinct leads. Say "N leads" and keep run counts for the error section, labelled as attempts.
- Grep the config for every quoted string before attributing it to the model.
- Check whether the behaviour is still happening — a defect fixed last week reads very differently from one that fired this morning.
- Pull one verbatim example per defect. A finding without a quote does not survive contact with ops.
