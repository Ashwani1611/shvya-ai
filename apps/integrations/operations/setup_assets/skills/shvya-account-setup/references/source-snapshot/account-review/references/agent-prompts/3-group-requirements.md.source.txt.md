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
