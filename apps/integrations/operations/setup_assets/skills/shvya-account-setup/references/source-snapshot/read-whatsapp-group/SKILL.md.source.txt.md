---
name: read-whatsapp-group
description: >-
  Read (never send) the messages in a client's WhatsApp group, or any WhatsApp
  chat, through a Kraya ops team member's hosted WAHA session. Use this whenever
  someone asks to read, summarise, catch up on, check, or pull the conversation
  of a WhatsApp group or chat "using this phone number", "from Diksha's number",
  "on the ops account", or names a client support group (e.g. "Kraya | Acme",
  "Acme x Kraya"), even if they only say "what did the client say in the
  group yesterday" or "read the last 50 messages in the Urbannest group". Works
  with a group name plus the ops number the group is on. Read-only; sending
  messages is a different skill.
---

# Read a client's WhatsApp group through the ops account

Ops team members run their WhatsApp numbers as hosted WAHA sessions on the Kraya ops account. Every client support group ("Kraya | Renewal", "Acme x Kraya Support") lives on one of those numbers. This skill resolves *which* session, finds the group by name, reads its messages through Kraya's own API, and renders a readable transcript. It never sends anything.

## Inputs

| Input | Required | Notes |
|---|---|---|
| Group (or chat) name | yes | Partial names work; the API searches by substring. If the user gives a phone number instead of a group, treat it as an individual chat |
| Ops phone number, or the team member's name | usually | The number the group is on. Bare 10-digit Indian numbers get `91` prefixed. If omitted and the group name is unique across running sessions, the script searches every running session and asks only when it finds the group on more than one |
| Range | no | `--limit N` (default 100 messages), `--since YYYY-MM-DD`, `--until YYYY-MM-DD` (IST) |

## Access

| Item | Value |
|---|---|
| Token | `KRAYA_OPS_ACCOUNT_TOKEN` in the environment: a bearer token for the Kraya ops account that owns the hosted sessions. Never print it, never write it to a file or a reply |
| Base URL | `KRAYA_API_BASE_URL`, default `https://api.kraya-ai.com/api` (production) |
| Local fallback | when running on the ops laptop with no env var, the token is in `.claude/creds.md` under the label "bearer token for kraya ops account that has hosted accounts of all the ops team members"; the file holds several tokens, so match the label exactly |

## Steps (the script does all of this; read them to understand its output and errors)

1. **List sessions.** `GET /waha/sessions` → `{ success, sessions: [ { public_id, phone_number, display_phone_number, status, owner_name } ] }`. Match the requested phone by digits against `phone_number`, or the name against `owner_name`. Readable statuses are `running` and `syncing`; anything else (`starting`, `qr_ready`, `stopped`, `failed`) means the number is not connected right now, so say that rather than retrying.
2. **Find the group.** `GET /waha/sessions/{public_id}/chats?q=<name>&limit=50` → `{ success, data: [ { id, name, phone, lastMessage } ], has_more }`. Groups have ids ending in `@g.us`, individual chats end in `@c.us` (or `@lid`). Exact name match wins; otherwise show the candidates (name + id) and ask. Never guess between two plausible groups: their contents are different clients' confidential conversations.
3. **Read messages.** `GET /waha/sessions/{public_id}/chats/{chatId}/messages?limit=100&offset=0` → `{ success, data: [ { id, timestamp (unix seconds), from, fromMe, senderName, participantName, body, caption, type, hasMedia, media: { url, mimetype, filename } | null, replyTo, ack, ackName } ] }`, newest first. Page with `offset` until the count or date range is satisfied. `fromMe: true` is the ops member's own number; `senderName` is the participant's WhatsApp display name; `body` is empty on media-only messages, where `caption` and `type` describe what was sent.
4. **Render.** Chronological, IST timestamps, one line per message: `[08 Sep 14:31] Abhyudaya Srinet: check in to buy more credits [image]`. Then summarise if asked. Quote verbatim only the messages that answer the question.

## Run it

```bash
python3 .claude/skills/read-whatsapp-group/scripts/read_group.py \
  --group "Kraya | Renewal" --phone 9876543210 --limit 50
```

Other selectors: `--owner "Diksha"` (team member's name) or `--session hzZrEhge` (a `public_id` you already know). Add `--since 2026-09-01 --until 2026-09-08` for a window, `--json` for raw messages instead of a transcript, `--list-sessions` to just print the sessions, `--list-groups` to print the session's groups without reading any.

The script prints the transcript to stdout and nothing else; errors go to stderr with a non-zero exit. It uses `curl` under the hood (the Kraya API is fine with Python's TLS, but curl behaves the same on every ops machine).

## Rules

- **Read-only.** Never call `send-message`, `send-file`, or any POST on these routes from this skill. If the user wants to reply, hand off to the sending skill after showing them the transcript.
- **Confidential.** Group content is a client's private conversation with the ops team. Answer the question asked; do not paste whole transcripts into shared channels, and never include messages from a different group than the one requested.
- **Live reads hit the WAHA worker.** Keep it to what the question needs (a few hundred messages at most), never poll, and treat a 500 "Something went wrong while fetching chat messages" as the session being down rather than the chat being empty.
- **Ambiguity stops the read.** Two groups match, two sessions match, or the number is not connected: report the candidates and ask. Reading the wrong client's group is worse than asking one question.
- **Premium gate.** These routes sit behind `free-tier-limits:premium_features`; the ops account is premium, so a 403 means the token is for the wrong account.

## Source of truth

`Kraya-Laravel/routes/api.php` (`waha/sessions`, `waha/sessions/{public_id}/chats`, `.../chats/{chatId}/messages`), `WahaController@listSessions`, `@getChats`, `@getChatMessages`, `WahaService@formatChatMessagesForApi`. Shapes above were verified against production on 2026-09-09.
