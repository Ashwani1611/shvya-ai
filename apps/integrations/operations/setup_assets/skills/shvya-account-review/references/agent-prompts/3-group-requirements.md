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
