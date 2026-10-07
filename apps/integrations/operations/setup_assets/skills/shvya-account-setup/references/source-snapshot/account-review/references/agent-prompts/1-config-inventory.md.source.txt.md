# Subagent prompt 1 — config inventory

Fill the bracketed fields and dispatch. Runs concurrently with prompts 2 and 3.

The point of this prompt is an inventory you can audit against, not a summary. Two instructions carry most of its value: **reproduce the org info verbatim**, because every later check greps it, and **say `NONE` explicitly**, because a missing integration reported by omission is indistinguishable from one the subagent forgot.

---

Audit the full Kraya configuration for production organization `[ORG_ID]` ("[ORG NAME]"), a [one-line description of the business]. Sold [DATE], [AMOUNT], pack `[PACK]`, sales rep [REP], ops POC [POC].

TOOLS: [state what is available — the Kraya REST API with the client's token, and/or read-only production MySQL. If MySQL: load the tool first, validate every column against `Kraya-Laravel/.cursor/rules/database-schema.mdc` before querying, and never write. Note that the `leads` table has no `deleted_at` column.]

What the client told us, which is the baseline this is audited against: [the stated business model, team size, AOV, lead volume, lead sources, and whether the goal is appointments or direct sale].

Report all of:

1. **Org** — name, pack, tags JSON (say which pack booleans are actually set), is_disabled, created_at, credits balance and used, timezone, industry, seats, onboarding_completed and onboarding_completed_at.
2. **AI config** — the org info reproduced **verbatim and in full** (`about`, `qualification_requirements`, `bot_languages`, `attachments`, `sendable_files`). Plus the prompt config: qualification template, model, top_k, run_qualification_check, interactive_options_enabled, and **when it was last updated**.
3. **Knowledge base** — FAQ categories and articles: count, titles, created dates. Flag any that are generic onboarding-template boilerplate rather than bespoke to this business, and quote the giveaway line. Attachments and sendable files with their descriptions.
4. **Sequences** — every sequence: name, enabled, mode, created date, and each step (type, delay, ~120-char content preview). State explicitly which are bespoke and which are template seeds, and **how many leads each is actually assigned to**.
5. **Rules** — every rule: name, enabled, trigger, conditions, action, processing order. **Count rule executions for this org.** Flag any sequence with no live rule to start it, and any rule whose trigger conditions are empty.
6. **Pipelines and stages** — names in order, with the AI on/off switch per stage.
7. **Integrations** — the client named [SOURCES]. Check those first, then every other integration table. **Say `NONE` explicitly where zero rather than omitting the row.**
8. **[The feature this client's funnel depends on]** — [e.g. appointment booking: is a calendar configured, enabled, what availability, and how many leads have actually booked]. Dig in; this is the one that matters most for them.
9. **Channel** — hosted sessions and Cloud API accounts: phone, status, created date. Which channel is live, and when it was connected relative to the sale.
10. **Leads** — total, by source, by stage, and daily counts since [DATE]. Identify seeded sample leads separately. If the total looks implausible against the client's stated volume, **explain where it came from** before reporting it.
11. **Activity** — credit consumption by type and by day, distinct leads that received an AI reply, and the latest health-score row with all columns.
12. **Users** — name, email, role, created, last login.

OUTPUT CONTRACT — group under these exact headings, under [130] lines:
`### 1. Org` `### 2. AI config` `### 3. Knowledge base` `### 4. Sequences` `### 5. Rules` `### 6. Pipelines` `### 7. Integrations` `### 8. [feature]` `### 9. Channel` `### 10. Leads` `### 11. Activity` `### 12. Users`

Terse factual bullets with real values. The org info goes in a fenced block, complete and unedited — that is the single most important output, and a summary of it is useless. Write `NONE` for anything absent. Finish with `### Key gaps worth flagging` (at most six, most severe first) and `### Table names used`, so the findings can be re-queried.
