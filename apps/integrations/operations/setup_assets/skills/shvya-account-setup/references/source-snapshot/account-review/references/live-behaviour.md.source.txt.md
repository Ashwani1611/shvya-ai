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
