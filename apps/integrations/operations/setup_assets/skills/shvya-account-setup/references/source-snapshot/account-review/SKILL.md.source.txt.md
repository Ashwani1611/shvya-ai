---
name: account-review
description: Review a live Kraya account end to end and decide whether its setup is complete and actually working. Use whenever someone asks to review, audit, check, analyse or "look at" a client's account or org, pastes an analytics dashboard URL or an org id and asks what is wrong with it, asks whether an account was set up correctly or covers what the client asked for, asks why a client is not getting value or is about to churn, asks what the AI is really saying to that client's leads, or asks which features a paying client is not using. Covers requirement coverage against the client's own words, whether the configuration is switched on, what the bot actually said to real leads, where it will hallucinate, and what is being paid for but never used.
---

# Kraya account review

You are auditing a paid, live Kraya account. The question you answer is not "is there configuration here" but **"does this account do what the client asked for, and is it actually doing it right now."** Those are different questions and accounts fail them independently: the best-written qualification spec in the fleet is worthless with its rules switched off, and a fully-firing account can be confidently telling leads things the client never approved.

Three findings from real reviews set the shape of this skill.

- An account had a 28,000-character bespoke prompt, 16 stages, 5 sequences and 19 automation rules. **Every bespoke sequence and 19 of the 21 rules were disabled, and `rule_executions` was zero.** The only things live were generic onboarding templates promising discounts the client had explicitly forbidden. A configuration review would have called it excellent.
- Another account was configured superbly and was booking consultations at four times the client's historic rate. It was also telling leads *"we'll have Mansi call you at 1:30 PM"* for a solo practitioner who had no idea those calls existed. Nothing in the config was wrong; only the live traffic showed it.
- In that same account the bot quoted prices the client had said to withhold. The obvious conclusion was hallucination. It was not: the prices were in `qualification_requirements`, put there by a later change. **Calling it a hallucination would have sent ops to fix the wrong layer.**

So: gather what the client asked for from their own words, inventory what exists, check what is switched on, read what the bot actually said to real leads, and verify every finding against the layer that produces it before you write it down.

## Reference files (read the one you need, when you need it)

| File | Read it when |
|---|---|
| `references/evidence-sources.md` | at the start. Where each class of fact lives, what you can reach in this environment, and the access recipe per source |
| `references/review-checks.md` | the check catalogue, A–G. This is the body of the review |
| `references/live-behaviour.md` | for the production-traffic sweep: pulling an org's runs, and the probes that find invented links, prices, times and promises |
| `references/known-traps.md` | **before you report any finding.** The false positives that look damning and are not. Read this at least once per review |
| `references/agent-prompts/1..3-*.md` | the three fan-out prompts, ready to paste |
| `kraya-account-setup/references/conflict-audit.md` | for static contradictions inside the config: fact ledger, promise-without-material, stale seeded content. This review calls that audit rather than repeating it |
| `kraya-account-setup/references/langsmith.md` | trace access and run shape. `live-behaviour.md` builds the population sweep on top of it |
| `kraya-account-setup/references/api-reference.md` | every read endpoint and response envelope |
| `kraya-account-setup/references/industry-templates.md` | to recognise seeded onboarding content you are about to mistake for bespoke work |
| `read-whatsapp-group` skill | to read the client's support group |

## Ground rules

1. **A finding is a claim until you have opened the layer that produces it.** Before calling anything a hallucination, grep the org info, the FAQs and the sequence copy for the exact string. If it is there, the defect is in the configuration, not the model, and the fix is somewhere else entirely. This rule has caught more bad findings than any other.
2. **Count distinct leads, never runs.** Retries, regenerations and queue replays inflate run counts by one to two orders of magnitude. One review found 121 failed generations and reported an outage; they were 2 leads retried sixty times each, and both got their reply. Every population number in your report is a distinct-lead number, or it is labelled as attempts.
3. **Separate "not configured" from "configured and off" from "configured, on, and never triggered."** They have different owners and different fixes. An empty stage whose rule is enabled means the funnel never reaches it; a full stage whose rule is disabled means someone switched it off. Report which.
4. **The client's own words outrank the call summary, which outranks the CRM card.** Requirements drift as they are relayed. Quote the client verbatim and cite where the quote came from, so ops can see what was promised versus what was written down.
5. **Attribute writes are asynchronous and the AI cannot make them.** The reply schema carries a message and a stage change and nothing else. An attribute missing on a lead whose stage moved is normal platform behaviour, not an ops error. Never raise it as a setup defect. See `known-traps.md`.
6. **Report what works with the same rigour as what does not.** A review that lists only failures gets discounted, and ops stops reading. Name the parts that are genuinely good and the numbers that prove it.
7. **Every recommendation names a layer and an owner.** "The AI shouldn't do that" is not actionable. "Add a rule on entry to Retail Enquiry to switch AI off — ops, 10 minutes" is.
8. **Read-only.** This skill never writes to the account. When a fix is obvious, propose it and hand it to `kraya-account-setup`; do not apply it here.

## Keep your context small: fan out, then merge

A full review reads a 60,000-character prompt, five call transcripts, a few hundred WhatsApp messages and a few thousand AI runs. Pull all of that into one context and you will lose the thread and start inventing. Fan out three subagents in a single message so they run concurrently, and keep the production-traffic sweep in the main thread, because that is where the judgement calls are.

| Subagent | Prompt | Returns |
|---|---|---|
| Config inventory | `agent-prompts/1-config-inventory.md` | the full account state, org info reproduced verbatim, and explicit `NONE` for everything absent |
| Call requirements | `agent-prompts/2-call-requirements.md` | one section per call, then a numbered **consolidated requirement list** — the audit baseline |
| Group requirements | `agent-prompts/3-group-requirements.md` | timeline, config requests with verbatim quotes, complaints, promises and whether delivered, silence gaps |

**Give every subagent the output axis.** Each one sees a slice; you write along a different axis. Tell each to group its findings as a numbered list of discrete, checkable requirements, because the merge you are about to do is requirement by requirement.

**Then merge deliberately, and check the seams.** A gap (nobody covered it) gets a targeted follow-up subagent, never a guess. A contradiction between two reports gets settled by opening the code or the data, never by averaging or by trusting the more confident one. A duplicate — the same root cause reported three ways — collapses to one item before it reaches the reader.

## Phase 0: identify the account

Resolve the org id, from an analytics dashboard URL (`?org_id=…`), the org name, or the CRM card. Then pull the frame the rest of the review hangs on:

- **Sale**: pack sold, amount, date, rep, whether payment is partial.
- **Ops CRM card**: current stage, how long it has sat there, flags (`iterations_requested`, `awol`, `ongoing_issue`, `paused`), POC, `handed_over_at`, and the BAC notes or attributes holding the client's stated numbers.
- **Account age versus activity**: org `created_at`, the date the first real lead arrived, and the date the channel was connected. The gap between the sale and the first real lead is the single most useful number in the review; one account had sixteen days of paid time before anything worked.

State the elapsed time and the stage on the card before anything else. A card reading "Account Setup Completed" on an account with no channel connected is itself the headline.

## Phase 1: fan out, and sweep the traffic yourself

Dispatch the three subagents in one message. While they run, do the production-traffic sweep from `live-behaviour.md`: pull every root run for the org, split real traffic from playground demos, and get the distinct-lead counts, the stage-change decisions, the error profile and the reply-latency distribution. This is the part that finds defects nothing else can see, and it is why it stays in the main thread.

## Phase 2: run the checks

Work `references/review-checks.md` in order. A–C decide whether the account is complete, D–E decide whether it is correct, F–G decide whether it is worth what the client pays.

| | Check | Answers |
|---|---|---|
| A | Requirement coverage | does the config do what the client asked for |
| B | Activation | is it switched on, and has it ever fired |
| C | Channel and funnel reachability | can leads get in, and can they get all the way through |
| D | Live behaviour | what did the bot actually say to real leads |
| E | Ambiguity and hallucination surface | where will it invent, and which facts have two values |
| F | Feature utilisation | what is paid for and unused |
| G | Hygiene and commercial | tags, pack, credits, seats, renewal exposure |

## Phase 3: verify before you write

For every finding, name the evidence and the layer. Open the file, the row or the run. Two reports disagreeing gets resolved by reading, never by averaging. Anything you could not verify is reported as unverified, with what it would take to settle it — that is a legitimate and useful finding, and dressing it up as certainty is not.

Run `known-traps.md` over your draft list and delete what it catches. On a typical review it removes two or three items that looked like the worst problems in the account.

## Phase 4: report

Lead with the verdict in two or three sentences: is this account complete, is it functional, and what is the single most damaging thing in it. Then:

1. **Timeline** — sale to now, with the gaps called out.
2. **Requirement-by-requirement table** — one row per requirement from the consolidated list. Columns: what they asked for, configured?, live?, evidence. This is the core deliverable and the thing ops acts on.
3. **Defects verified in live traffic** — each with a verbatim example, the affected distinct-lead count, the date range, and whether it is still happening.
4. **Structural gaps** — dark funnel halves, orphaned sequences, unwired integrations, unreachable stages.
5. **What is genuinely working** — with the numbers.
6. **Ranked actions** — each naming the layer, the owner and the rough effort.

Quote the client verbatim when reporting what they asked for, and quote the bot verbatim when reporting what it said. Paraphrase loses the thing that makes the finding actionable, and ops will not act on a summary of a summary.

End with anything the review could not settle, and anything that looked like an account problem but is a platform problem — those belong in a different queue and saying so saves ops a day.
