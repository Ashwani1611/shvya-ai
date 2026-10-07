# Reference contents

- Review checks A–G
- A. Requirement coverage
- B. Activation
- C. Channel and funnel reachability
- D. Live behavior
- E. Ambiguity and conflict
- F. Feature use and outcomes
- G. Operational and commercial hygiene
- Complete review method from supplied reference
- The check catalogue
- A. Requirement coverage
- B. Activation
- C. Channel and funnel reachability
- D. Live behaviour
- E. Ambiguity and hallucination surface
- F. Feature utilisation
- G. Hygiene and commercial

# Review checks A–G

## A. Requirement coverage

Build one numbered list of discrete, checkable requirements. Include requirements stated once, and mark later iterations with their date. Quote precise wording for disclosure, qualification, timing and handoff rules. Track the lineage when a call summary was forwarded into a group so it is not counted twice.

For each requirement record requested behavior, configured object/field, active state, observed outcome and evidence. Check the downstream consumer: a stage exists, but does a supported active Workflow react to entering it? Distinguish not configured, configured/off, active/no observation, observed/correct, wrong configuration and unavailable evidence. Compare conflicting thresholds, hours, units and delays side by side; do not silently choose the configuration or newest timestamp without business authority.

## B. Activation

Read actual Shvya `is_active`, AI switches, sender/provider bindings and supported trigger/action semantics. Check Workflow/Cadence dependencies and inactive or orphaned references with integrity diagnostics. A present definition is not execution evidence. Use runtime counters and per-lead traces to show observed execution, with their time windows.

Do not import the source system's claim that a sequence `enabled` field has no effect. Shvya controls must be judged from its canonical tools and validator. A zero count for a limited window does not prove a feature never ran. Check whether generic draft copy is active where bespoke copy was expected and whether AI remains enabled at a human-owned handoff stage.

## C. Channel and funnel reachability

Validate active WhatsApp account to pipeline mapping, duplicates/unbound accounts, provider connection health and applicable messaging switches. Check sources named by the client only against supported integration evidence; mark unavailable connectors explicitly. An apparently connected account with invalid routing can still fail to create or route leads.

Follow each important path into and out of its stages, including post-handoff and post-conversion paths. Use aggregate stage/source mix where returned, bounded stalled cohorts and sampled snapshots/traces. Empty stages do not alone prove broken routing: consider recent activation, no eligible demand, small sample and incomplete history. Imported/backfilled/test leads are not organic growth; exclude them only when source evidence supports the classification.

## D. Live behavior

Use `live-behaviour.md`. Check unapproved figures, unsupported services, corrupted links, callback/booking commitments, repeated questions, duplicate messages, human/AI interference, opt-out/handoff continuation, delivery failures and response timing.

Every defect needs a concrete message reference/quotation, affected distinct-lead count in the observed scope, date range and current/historical status. A current draft config cannot by itself explain a past reply. Do not label an outbound message AI-generated when attribution is missing.

## E. Ambiguity and conflict

Run the setup conflict audit and compare canonical Playbook qualification with FAQs, About, Touchpoints, Cadences and supported settings. Identify multiple values for a fact, missing units, overlapping branch rules, expired offers, promised files that are not available, and instructions to perform actions unavailable in the runtime.

Backend qualification, tenant isolation and CRM execution rules remain authoritative. A Playbook saying “mark Qualified immediately” cannot replace required answer evidence. Avoid replacing qualification-owned sections with free-form prompt edits when structured qualification tools own them. Redacted/truncated content requires an evidence limit, not a guessed replacement.

Measure prompt length against actual stored/compiler limits and duplication; do not repeat the legacy unproven 20k/50k reliability thresholds. A coherent long prompt may still need simplification, but unsupported character thresholds are not defects. Native policy simulation validates deterministic configuration; it does not prove natural-language replies are good.

## F. Feature use and outcomes

Compare the client's objective with evidence of actual outcomes from conversion analysis and supplied baselines. Examine necessary integrations, usable channels, follow-up timing and saved replies. Report unused features only when the relevant entitlement and usage are known; missing MCP visibility is not zero use. Calendar, voice, seats, billing and app/device constraints may need supplied records or an integration owner.

Compare equivalent periods and disclose source/stage mix changes, test/import exclusions and response-time definitions. An improved conversion rate is an association unless attribution supports causality. No activity in a newly activated path is not enough to claim wasted subscription value.

## G. Operational and commercial hygiene

Check conflicting onboarding status, unresolved incidents, stale copy and dependency integrity through available reads. Use supplied contract/sale records for plan, renewal, commercial promises, payment and seat claims; do not infer a plan from tags that the tools did not return. Deduplicate sale evidence and distinguish invoice, payment and entitlement.

Credit runway requires both a reliable balance and a comparable consumption interval; otherwise mark it unavailable. Login/activity health is not requirements compliance. End with measurable working behavior and ranked actions naming the producing layer, responsible owner, evidence and reasonable effort/uncertainty. No actions execute in this review.


# Complete review method from supplied reference

> SHVYA ADAPTATION: Read runtime-contract.md before using this full reference. Preserve this method's detail, but compile its output into native SHVYA schemas. Kraya field names, API routes, database queries, token scripts and past performance claims are historical context, not live capabilities. Sample businesses, prices and policies remain examples. Human handoff/opt-out takes precedence over continued qualification; use verified double-brace CRM tokens and provider bindings. This reference does not authorize sending, enrollment or activation.

# The check catalogue

Run A–G in order. A–C decide whether the account is **complete**, D–E whether it is **correct**, F–G whether it is **worth what the client pays**. Every check names the evidence that settles it.

---

## A. Requirement coverage

The core deliverable. Take the consolidated requirement list the call and group subagents returned, merge them into one numbered list, and put every requirement in a table with four columns: **what they asked for / configured? / live? / evidence**.

Two columns, not one, because they fail independently and have different owners. A requirement can be:

- **configured and live** — the only passing state
- **configured, not live** — built then switched off, or its trigger deleted. Ops fix, minutes
- **not configured** — never built. Ops fix, hours
- **configured wrongly** — built, running, and doing something other than what was asked. The most damaging state, because it looks fine on every dashboard

Rules for building the list:

1. **Quote the client verbatim.** "AI must not quote prices" is weaker evidence than *"I generally don't want AI to share the pricing upfront"*, and ops will argue with the first and act on the second.
2. **Mark requirements that arrived after go-live** as iterations, with their date. These are the ones most likely to be half-applied, because they land as chat messages rather than a spec.
3. **Requirements the client stated once and never repeated still count.** A client who mentions their second location on the assessment call and never again has still told you their funnel needs it.
4. **Check the read side of every write.** A stage the AI can move a lead into is only useful if something downstream consumes it. A requirement is not met by a stage existing.
5. **Where the config and the requirement differ on a number** — hours, thresholds, minimums, delays — report both values side by side and ask which is right. Do not assume the config is the error; the client may have changed their mind in a channel you have not read.

---

## B. Activation

The check that separates a real review from a configuration listing. An account can hold a flawless build with none of it running.

| Ask | Evidence | What a failure looks like |
|---|---|---|
| Has any rule ever fired? | `rule_executions` count for the org | **Zero.** The entire automation layer is decorative |
| Which rules are enabled? | `rules.enabled` per row | 19 of 21 disabled while 3 generic template rules run |
| Does every sequence have a live trigger? | join each sequence to the rule that starts it | An orphan: sequence intact, its rule soft-deleted months ago, 0 leads assigned |
| Which sequences are actually assigned? | per-sequence lead counts | Bespoke sequences at 0 while seeded template sequences carry live leads |
| Are template seeds still live? | compare against `industry-templates.md` | Generic "special discount" copy running on an account whose prompt forbids discounts |
| Is the AI switched on where it should be, and off where it should not? | `stages.ai_switch`, per-user AI toggles | AI still replying on stages a human owns |

`auto_responder_sequences.enabled` is a **no-op** — gating happens at the rule and assignment level. Never report a sequence as inactive on the strength of that column; check whether a rule starts it and how many leads carry it.

When a bespoke build is off and template seeds are on, that is the headline of the review, not a bullet near the bottom.

---

## C. Channel and funnel reachability

**Can leads get in?**

- Is a channel connected at all — `waha_sessions` for hosted, `whatsapp_accounts` for Cloud API, `instagram_accounts` for IG? An account with neither has never run, whatever the CRM card says.
- When was it connected, relative to the sale? That gap is dead paid time and belongs in the timeline.
- Are the client's stated lead sources actually wired? A client naming Meta ads, Google ads and IndiaMART with zero integration rows is paying for attribution they do not have. **Worse, if the org info names those sources as live, the bot will discuss channels the account cannot receive on.**
- Are there real leads, or only seed data? A small round number of leads all created on the org's creation date with `source = crm` is the sample set. Real lead count is the number that is not those.

**Can leads get all the way through?**

Take every stage and ask what puts a lead there and what happens next. Stages holding zero leads are the tell: either nothing routes into them, or the thing that should is disabled.

The common failure is a **dark funnel half** — the early stages full and firing, everything past the hand-off point empty, so the post-conversion sequences the client paid for have never run once. Check the back half explicitly; it is invisible from the volume numbers, which all look healthy.

---

## D. Live behaviour

What the bot actually said to real leads, from the production-traffic sweep in `live-behaviour.md`. Nothing else in the review finds these.

Report per defect: a **verbatim example**, the **distinct-lead count**, the **date range**, and **whether it is still happening**.

The probes that have found real defects:

- **Invented URLs.** Extract every link the bot has sent and check each against the account's real slugs. Models corrupt a correct URL on regeneration — one account sent three spellings of its own booking link, two of them dead.
- **Commitments the client cannot keep.** Search for the bot promising a specific time, date, callback or person. One account was booking a solo practitioner's day with leads who then arrived expecting a call she knew nothing about.
- **Figures the client said to withhold.** Prices, fees, discounts, timelines. **Then grep the config for the figure before calling it anything** — see ground rule 1.
- **Capabilities outside the catalogue.** The bot confirming a service the business does not offer. Check against the `about` block's exclusions.
- **Contradicting the client's own staff.** On shared numbers a human and the bot both reply. Look for the bot reversing something a human just told the lead — it is invisible to every other check and is the most embarrassing failure mode with a customer.
- **Repetition and duplicate sends.** Identical text to the same lead more than once.
- **Latency.** Median and tail, split by whether the lead wrote inside the configured hours. A good median hides a tail where out-of-hours leads wait hours.
- **Errors.** Group by distinct lead, not by run, then ask whether each affected lead ever got a reply. Most error spikes are retries and cost the client nothing.

Also check **who else is on the channel**. Where a human team shares the number, separate their messages from the bot's — anything sent that is not in the model's output set is a human. If both are answering the same conversations, say so and count the overlap; no rule exists to stop the AI when a human takes over unless someone built one.

---

## E. Ambiguity and hallucination surface

Where the config will make the bot invent. Run `kraya-account-setup/references/conflict-audit.md` for the fact ledger, promise-without-material and stale-content checks, then add the review-specific ones below.

**Facts with more than one value.** The ledger catches these across sources; pay special attention to values the bot states out loud — a consultation fee appearing as three different numbers in the org info, the FAQs and a live reply means every lead gets a different answer.

**Instructions the platform cannot execute.** A rule the model is structurally unable to follow is worse than no rule: it reads as compliance and produces drift. The known class is **attribute ordering** — any instruction to write, verify or sequence an attribute write. The reply carries a message and a stage change and nothing else; attributes are extracted afterwards by a separate queued job. See `known-traps.md`.

**Rules with no material behind them.** Anything the config tells the bot to share, send or answer must exist somewhere it can reach — a value in the org info, an FAQ, or a sendable file referenced by UUID. Otherwise it invents. A prompt referencing a catalogue, brochure or price list with `sendable_files` empty is a guaranteed hallucination.

**Size and precision.** `qualification_requirements` beyond roughly 20,000 characters starts costing reliability; beyond 50,000 it is catalogue stuffing and compliance degrades in ways that are hard to predict and harder to test. If the spec is that large, say so, and treat any behavioural defect in it as likely a symptom rather than a missing rule.

**Settings that contradict the prompt.** The recurring one: `interactive_options_enabled` off while the whole spec is built around numbered option lists, so the lists render as plain text. Check the prompt config's last-updated date — never touched since signup, against a heavily-iterated prompt, means nobody revisited the switches.

**Ambiguity proper.** Flag instructions that a careful reader could follow two ways: thresholds without units, "share it later" without a trigger, conditions that overlap, an edge case whose rule contradicts a numbered rule earlier in the same document. These are where behaviour drifts between leads for no visible reason.

---

## F. Feature utilisation

What the client pays for and does not use. Compare features adopted against features used on the latest `organization_health_scores` row, then check the ones that matter for this client's funnel:

- **Booking** where the goal is appointments — calendar configured, enabled, reachable, and actually producing bookings? Availability matching the stated hours, including every day they said they work?
- **Integrations** — every source the client named on the call.
- **Broadcasts, templates, email** — available on the pack, never used?
- **Co-Pilot**, bump-ups, quick replies, AI calling.
- **Seats** — paid for and unassigned.

Then the question behind the check: **is this client getting the outcome they bought?** Compare against their own stated baseline — the leads per month, conversion rate and appointment count they gave on the assessment call. An account booking four times the client's historic rate is a retention story worth writing down as plainly as any defect.

Note anything the client physically cannot use. One client worked exclusively from an iPad, which ruled out the Chrome extension and the Android app permanently — every workflow had to exist in the web dashboard, and nobody had noticed.

---

## G. Hygiene and commercial

Small, fast, and each one has bitten a real account:

- **Pack tags versus the sale.** `organizations.tags` booleans decide what the client gets. A DFY sale tagged `industry_essentials` gives them the wrong product.
- **Industry** set to something unrelated, which steers seeded content.
- **Credit burn rate** against the balance: days of runway. Flag it before it runs out, and check whether the client was told about the limit at sale — that is a live complaint on more than one account.
- **Renewal date and partial payment** — who is exposed and when.
- **Users and last login.** A client who has not logged in for a week is not reviewing anything the bot does.
- **Lead-count anomalies.** A sudden jump of thousands is almost always a channel backfill on connection, not real volume. Confirm where it went — if quarantined in a hidden stage, it is harmless and should be explained rather than reported as growth.
- **Onboarding completion** flags that disagree with each other, and CRM stage versus reality.
