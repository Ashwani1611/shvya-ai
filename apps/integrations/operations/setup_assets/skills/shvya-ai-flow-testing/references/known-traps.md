# Known traps before reporting

- A missing attribute after a stage move is not automatically normal or automatically defective. Use `diagnose_lead_qualification`, `get_ai_diagnostics`, workflow/message traces and canonical configuration to identify extraction delay, missing evidence, permission or execution issues. The source system's assertion that the AI can never write attributes is not a Shvya API contract; administrative attribute writes and conversation runtime behavior are distinct.
- Legacy sequence `enabled` semantics do not carry over. Read Shvya active flags and actual trigger/dependency/execution evidence. A configured object may be inactive; a zero bounded counter does not mean it never fired.
- Native `get_conversation` does expose recent Shvya WhatsApp/Instagram messages. Do not repeat the source claim that hosted messages exist only in external traces. Absence in a bounded/redacted read still does not prove no message ever existed.
- Seeded test leads, channel backfills and imports can inflate counts. Identify them from actual provenance, not a round total, creation date coincidence or guessed database flag. The current stage is not necessarily the stage at the moment of an incident.
- A high activity/health score is not proof of correct client-policy behavior. Simulated qualification success is not a live generated response or delivery test.
- Retries inflate attempts. Count distinct leads and inspect recovery/delivery before describing an outage. Failed attempts may carry cost or delay, but neither can be quantified without suitable evidence.
- Test/demo/replay traffic can mimic production. Filter only using reliable recorded markers; mark uncertain cases and avoid inventing environment or template fields absent from native results.
- Dates require explicit timezones. Compare incident time, observation time and current state separately. Do not query a local-looking time as UTC or treat a partial-day window as a full period.
- Group exports may begin when a participant joined or when the export was made. State earliest/latest included messages and known gaps. `fromMe` does not identify the Shvya side. Uninspected voice notes, screenshots and attachments are unknown evidence.
- A recording title, duration zero or transcript-list entry does not establish usable content. Read an authorized supplied transcript before drawing conclusions. Missing pre-sale commitments are a process/evidence gap, not proof nothing was promised.
- A price in an available policy source is a configuration-policy conflict, not proof the model invented it. Absence from metadata-only knowledge reads does not prove the model invented it either. Incident-time content can differ from current content.
- A client's diagnosis describes a symptom. Continued outreach after a correct terminal AI decision may come from a separate Cadence, Workflow or human. Verify the producing layer before assigning an owner.
- Two reports quoting the same call summary are one source. Reopen subagent references; confidence and repetition do not establish truth.
- Channel capabilities can legitimately change rendered behavior. Compare supported channel/runtime settings when exposed, and do not infer a backend error from a prompt's unsupported button/format request.
- Duplicate sale rows, subscription tags and invoice records can disagree. No native commercial data here means UNAVAILABLE. Do not import old source DB columns or declare entitlement from a guessed tag.
- Redaction and truncation are not empty configuration. Use the full supported AI read when possible; preserved redactions remain unknown and must not be overwritten by reconstruction.
- Inspecting a Workflow schema or generic object field does not authorize a new integration. Group reads, voice provisioning, raw recordings and an external vault remain capability gaps until real supported tools exist.


# Complete review method from supplied reference

> SHVYA ADAPTATION: Read runtime-contract.md before using this full reference. Preserve this method's detail, but compile its output into native SHVYA schemas. Kraya field names, API routes, database queries, token scripts and past performance claims are historical context, not live capabilities. Sample businesses, prices and policies remain examples. Human handoff/opt-out takes precedence over continued qualification; use verified double-brace CRM tokens and provider bindings. This reference does not authorize sending, enrollment or activation.

# Known traps

Findings that look damning and are not. Read this before reporting. On a typical review it deletes two or three items, usually the ones that looked like the worst problems in the account.

---

## Platform behaviour mistaken for a setup defect

**Attributes missing on a lead whose stage moved.** The reply schema carries a message and a stage change and nothing else — the AI cannot write an attribute. They are extracted afterwards by a separate queued job and are one turn stale even on the read side. So a lead sitting in a stage with the attribute behind that decision empty, or a `Qualification Status` of `Pending` on a qualified lead, is **normal platform behaviour on every account in the fleet**. Never raise it as an ops error.

The related finding that *is* real: org info that instructs the AI to write, verify or order attribute writes. That is an unexecutable instruction and belongs in check E.

**`auto_responder_sequences.enabled` gates nothing.** It is a no-op column; a sequence with `enabled = 0` still runs for its assigned leads. Judge a sequence by whether a live rule starts it and how many leads carry it.

**Hosted-WhatsApp and extension messages are not stored.** `whatsapp_messages` holds Cloud API webhook payloads only. An empty table on a busy account means the channel is hosted, not that nothing happened. Go to LangSmith.

**Seeded sample leads.** Signup seeds a small set with `source = crm`, all created in one batch on the org's creation date. They are not real leads and they are not a setup mistake. Subtract them before quoting any volume, and do not report them as activity.

**Channel-connection backfills.** Connecting Instagram imports every historical DM thread as a lead — thousands in a single day. It inflates the lead count and it is not growth, not a bulk-import error and not a billing problem. Check where they went: quarantined in a hidden, AI-off stage is correct handling and deserves one explanatory line, not a finding.

**Health score `Healthy`.** It measures usage intensity, not whether the setup matches the client. Accounts have scored in the seventies while telling leads things the client had forbidden. Never let it soften a defect, and never cite it as evidence the setup is right.

---

## Measurement artefacts

**Retry storms.** A failed generation is retried, and each attempt logs a run. One review counted 121 failures in a day and was about to report a major outage; they were **2 leads retried about sixty times each**, and both got a reply. Always collapse to distinct leads, then check whether each affected lead ever received a reply. The real finding in that case was wasted spend, not client impact.

**Playground traffic.** `request_mode = qualification-demo` is ops testing the bot, with no real lead behind it. Left in, it inflates reply counts and pollutes every probe. Filtered out, its volume is a useful signal about how much ops iterated.

**Replays and non-prod runs.** Filter `environment == "prod"`. Prompt-testing replays trace into the production project with real org and lead metadata and will double-count conversations and stage moves.

**The stage in `lead_data` is the stage at the start of the run**, not the outcome. To see where a lead ended up, read `change_stage` in the output, or the current row in the database. Reporting the input stage as the result understates every funnel.

**Timestamps.** The MySQL MCP renders through the host machine's local zone; a value can be hours off from what you expect. `CONVERT_TZ` to IST and, if it still looks wrong, run `date` and check the host's zone. CloudWatch windows need an explicit UTC epoch — a naive local `datetime` silently queries the wrong hours, and the logs that come back will look like a clean result.

---

## Source limitations that read as absence

**A WhatsApp group session only sees messages since that phone joined.** An early gap may be invisible rather than absent. Try several sessions, use the earliest, and state which session and which start date the timeline rests on.

**Voice notes and screenshots cannot be read.** Clients frequently send corrections that way, often at exactly the moments they are listing changes. Note the timestamps and say plainly that some requests may be inside them — an unread voice note is a known unknown, and silently omitting it makes the requirement list look complete when it is not.

**Fireflies list metadata reads `0`** for `num_sentences` and `duration` on list endpoints. Fetch the transcript before concluding a call is empty. Separately, a call can be genuinely empty: a recording exists with audio but no transcription. That is a finding about the record, not the account.

**A missing sales call is common.** Two consecutive reviews found the pre-sale promises existed nowhere in text. Report it as a process gap — it means nobody can audit what was sold.

---

## Attribution mistakes

**Config entries mistaken for hallucinations.** The single most expensive error available here. A price or a policy the client forbade, appearing in live replies, usually turns out to be *in* `qualification_requirements`, added by a change request after the original spec. Grep before you attribute. In the config: a policy deviation, owned by ops and the client. Absent from the config: a model defect, owned by the prompt. Different report, different fix, different team.

**The client's diagnosis is a symptom, not a cause.** One client reported the AI kept messaging leads who had said they could not travel. The AI was doing exactly the right thing — it asked, got a refusal, moved them to ineligible and stopped. The follow-up **sequences** kept firing, because no rule stopped them on that stage. Trace the actual mechanism before accepting the client's layer. Telling them the bot is fine and the automation is not is more useful than agreeing with them.

**Two subagents agreeing is not corroboration** when both read the same upstream summary. A call summary pasted into the WhatsApp group will be reported independently by the call agent and the group agent. Check whether two sources are actually independent before treating agreement as confirmation.

**A subagent's `file:line` or row is a claim.** Open it. Reports have named the right defect with the wrong cause, confidently. Where two disagree, read the code or the data; never average, and never take the more assertive one.

**DB flags versus runtime parameters can legitimately differ.** `interactive_options_enabled` may read on in the org config and arrive as off in the rendered prompt, because the channel does not support interactive replies. Check the value the model actually received before reporting a mismatch, and if the prompt then works around it with numbered lists, that is the system behaving correctly.

**Duplicate sale rows exist.** The same client can appear twice with the same amount and date, one row carrying an org and one not. Deduplicate before reporting revenue or counting new clients.

**The CRM card's pack can disagree with the sale.** The card reads org tags; the sale is its own record. When they differ, that is a real finding — the tags decide what the client gets — but report it as a tagging error rather than assuming either source is the truth about what was sold.
