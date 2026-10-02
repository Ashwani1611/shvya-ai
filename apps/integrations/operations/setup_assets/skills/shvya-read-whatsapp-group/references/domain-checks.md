# WhatsApp group analysis domain checks

## Evidence
Operate only on the explicitly supplied authorized export and verified active organization/chat identity. Record source ID, chat ID/name, participant roles, earliest/latest included timestamp, timezone, count, truncation and known missing history/media.

## Known traps
- No export means no live-group access through this skill.
- A session/export can begin after the real conversation started; silence before coverage is UNKNOWN.
- Direction/fromMe metadata does not prove a sender represents SHVYA.
- Voice notes/screenshots/media-only messages are content gaps until separately supplied/read.
- Forwarded summaries are not independent evidence of the original call/event.
- Duplicate message IDs must not inflate requirements.
- Similar group names do not establish identity.
- A customer request is not proof it was implemented; a SHVYA promise is not proof it was delivered.

## Verification
Extract discrete requirements/corrections/complaints/commitments with source references and timestamps. Separate request, promise, observed resolution and unresolved gap. Preserve partial-export limits.

## Handoffs
Requirements → account setup/review/Vault; actual lead chats use the canonical conversation diagnostic path rather than this skill.
