# Instagram domain checks

## Evidence
Separate OAuth redirect/state exchange, professional account binding, permissions/review/live-app state, webhook subscription, inbound event receipt, lead resolution, username/name mapping, AI execution, outbound eligibility and Send API result.

## Known traps
- OAuth consent screen success does not prove token/account binding completed.
- App Review/permission availability can differ for tester vs customer accounts.
- Instagram lead creation must not depend on phone.
- Username/IGSID is not a phone number and must not be forced into phone field.
- A phone shared later must be normalized/validated before mapping.
- Story replies/postbacks/media payloads differ from plain text.
- Generic integration lifecycle may support disconnect but not connect/reconnect; do not invent a tool.
- Correct inbox display does not prove AI auto-reply runtime.

## Verification
Trace one authorized DM from webhook to lead/conversation to AI/outbound provider response. Verify username/name capture and optional later phone mapping. Recheck lifecycle after connection changes.

## Handoffs
AI response → AI debugger; generic lifecycle → integration manager; CRM fields → lead repair/CRM architect.
