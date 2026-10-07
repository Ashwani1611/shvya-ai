# Subagent prompt 2 — call requirements

Fill the bracketed fields and dispatch. Runs concurrently with prompts 1 and 3.

Two instructions do the work: **quote verbatim**, because paraphrased requirements cannot be audited and ops will argue with them, and **end with a consolidated numbered list**, because that list is the baseline the whole review is built on. Without it you get five call summaries and the merge silently becomes yours.

---

Find and summarise every call recording involving the Kraya client "[CLIENT NAME]" — [one-line description]. They bought Kraya on [DATE] ([AMOUNT], pack [PACK]) via sales rep [REP] ([REP EMAIL]). Ops POC is [POC]. Their CRM card was created [DATE] and moved to [STAGE] on [DATE][, and carries a "[FLAG]" flag].

Expect a sales call around [DATE], a business assessment call between [DATES], an onboarding call after setup, and possibly later support calls where they asked for changes.

TOOLS: [the Fireflies tools available, and how to load them].
KNOWN GOTCHA: list metadata is unreliable — `num_sentences` and `duration` often read `0`. **Never conclude a call is empty from the list; fetch the individual transcript.**
Search several ways: title text (including misspellings and short forms of the business name), participant email, the rep's and the POC's names, and a date-range scan over [START] → [END] reading titles. Clients are recorded under inconsistent names.

For EACH call report:
- date, title, duration, participants and which side each is on
- **What the client asked for** — business model, services, customers, lead volume and sources, what they want the AI to do, what a qualified lead looks like to them, pricing and quotation policy, what the AI may and may not say, language, working hours, and [the mechanic this client's funnel turns on, e.g. exactly how appointments should be booked — slots, confirmation, deposits, rescheduling, no-shows]
- **What Kraya promised** — every commitment by the Kraya rep, with any timeline attached
- **Any change request, complaint, correction or dissatisfaction**[, especially the call behind the "[FLAG]" flag — capture exactly what they wanted changed]
- **Verbatim quotes with speaker names** for every configuration requirement. Quote generously; a paraphrase cannot be audited against a config.

OUTPUT CONTRACT:
One `### <date> — <call title>` section per call, oldest first, using the bold sub-headings above. Then a final `### Consolidated requirement list`: a numbered list where each item is **ONE discrete, checkable configuration requirement** phrased so it can be verified against an account (e.g. "3. AI must offer appointment slots only on Tue–Sun, 11:00–19:00"). Mark anything that arrived as a later change request **[ITERATION]**, and anything still unresolved **[OPEN]**.

That consolidated list is the most important output — it is what the account gets audited against. Under [160] lines.

If you find NO calls, say so explicitly and list every search you ran. If a recording exists but has no usable transcript, say that too and give the link — a lost sales call means the promises behind the purchase are unauditable, which is itself a finding.
