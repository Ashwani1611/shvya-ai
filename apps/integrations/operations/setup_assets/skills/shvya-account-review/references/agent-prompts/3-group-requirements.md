# Review agent 3 — WhatsApp support-group requirements

Fill the bracketed fields from verified context, then delegate. Task placeholders here are not native runtime variables.

Review the authorized support-group export [SOURCE FILE/ID] for [COMPANY NAME], organization [VERIFIED ORG ID], exact group/chat [ID AND NAME], over [DATE RANGE, TIMEZONE]. Known client/Shvya participants: [ROLES OR UNKNOWN]. Focus [OPEN ISSUE OR ITERATION].

Use the sibling `shvya-read-whatsapp-group` skill. Current Shvya MCP does not discover/read employee sessions or arbitrary support groups; `get_conversation` is for a known lead and is not a substitute. Work only from the supplied export. If unavailable or ambiguous, return an evidence gap and continue only source-independent work. No credential files, legacy API, external sends, context changes or account mutations.

Record original export/source, exact organization/group identity, earliest/latest included timestamps, message count, selected range and any truncation. Identify sides by confirmed participants, not `fromMe`. Preserve timezone information and flag ambiguous raw date formatting. Treat voice notes, screenshots and missing files as uninspected evidence with their message IDs/timestamps. Read linked attachments only if actually supplied or separately accessible within authorization; a link is not a grant of access.

Return:

1. Source/coverage statement, including excluded history or unknown roles.
2. A concise chronological timeline with precise message references and key quotations.
3. Numbered discrete configuration requirements, each with the client quotation and date; mark `[ITERATION]`, `[OPEN]` and explicit `[SUPERSEDED]` corrections.
4. Complaints/bugs: reported symptom, date, visible response/resolution evidence and unresolved status. Do not equate the customer's proposed cause with a technical diagnosis.
5. Shvya promises and whether delivery is visible in the supplied evidence. “Not observed” is distinct from “not delivered”.
6. Response/silence gaps only within demonstrably complete intervals; identify who was awaiting what, with uncertainty for partial history.
7. Whether the client was shown examples or asked to approve content, and exactly what was approved. A chat statement is not blanket permission to publish or send.

The requirement list is the main deliverable. Keep forwarded summaries linked to their upstream call so the reviewer does not count them as independent confirmation. Do not paste unrelated conversation content or infer missing media. End with evidence needed to settle unresolved items.


# Complete review method from supplied reference

> SHVYA ADAPTATION: Read runtime-contract.md before using this full reference. Preserve this method's detail, but compile its output into native SHVYA schemas. Kraya field names, API routes, database queries, token scripts and past performance claims are historical context, not live capabilities. Sample businesses, prices and policies remain examples. Human handoff/opt-out takes precedence over continued qualification; use verified double-brace CRM tokens and provider bindings. This reference does not authorize sending, enrollment or activation.

# Subagent prompt 3 — WhatsApp group requirements

Fill the bracketed fields and dispatch. Runs concurrently with prompts 1 and 2.

The group is usually the richest requirement source, because it holds the corrections the client sent after seeing the bot live — which is where most `[ITERATION]` requirements come from. It is also the only place you can see how long each side went quiet.

Use the `read-whatsapp-group` skill for access rather than rebuilding the recipe. The instructions below are what to do with what it returns.

---

Read the FULL WhatsApp support-group conversation for the Kraya client "[CARD NAME]" ([one-line description]) and report what was discussed.

GROUP JID: `[JID]@g.us`
Bought [DATE]. Ops POC is **[POC]**. The card sits in [STAGE] since [DATE][ and carries an "[FLAG]" flag — so somewhere in this chat the client asked for changes. Find exactly what they asked for].

ACCESS: use the `read-whatsapp-group` skill. Then, critically:
- **Each hosted session only sees messages since that phone joined the group.** Try several, especially [POC]'s, and use whichever has the EARLIEST history. Report which session you used and the date of its earliest message — an early gap may be invisible rather than absent.
- Ops members without a hosted session post from their own phones, so their messages arrive with `fromMe: false`. **Identify sides by `senderName`, never by `fromMe`.**
- Convert every timestamp to IST.
- Voice notes and screenshots cannot be transcribed. Note their timestamps and say so explicitly — clients often send corrections that way, and an unread voice note is a known unknown, not an absence.
- If the client shares a document link and you can open it, read it; correction docs shared in-chat routinely carry requirements that were never said out loud.

WHAT TO REPORT:
- **Chronological narrative**, day by day in IST, who said what. Quote **verbatim and generously** — the client's exact words are the point, and a paraphrase of a requirement cannot be audited.
- Flag specifically: (a) every configuration request or correction — tone, language, what to say and not say, pricing disclosure, qualification criteria, [booking rules], working hours, follow-up timing; (b) every complaint or bug report, and whether it was resolved; (c) every promise Kraya made and whether it was visibly delivered **in the chat**; (d) silence gaps on either side, with the gap in days and who was waiting; (e) whether the client was ever shown or asked to approve the AI's replies, sequences or knowledge base.

OUTPUT CONTRACT:
`### Session used` — session id, owner, earliest message date, messages read, and which sessions returned nothing
`### Timeline` — one `**DD-Mon**` heading per day with quoted exchanges. Under [200] lines
`### Client config requests` — numbered, each ONE discrete checkable requirement with the verbatim quote in parentheses. Mark change requests **[ITERATION]** and unresolved ones **[OPEN]**
`### Complaints / bugs` — a table: date, complaint, status as of the last message
`### Kraya promises and whether delivered`
`### Silence gaps`
`### Was the client shown or asked to approve anything`

The config-requests list is the most important output — it is what the account gets audited against. If you cannot reach the group, say exactly what you tried and the error you got; never invent content.
