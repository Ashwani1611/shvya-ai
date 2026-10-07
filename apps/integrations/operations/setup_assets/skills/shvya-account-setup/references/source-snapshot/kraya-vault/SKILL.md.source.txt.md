---
name: kraya-vault
description: Read a client's Kraya Vault (the content portal where clients dump brochures, links, photos, dictated notes and answers) and write facts back into it — from a Fireflies brainstorming transcript, the client's WhatsApp group, or what the ops rep says in chat — so the client only has to fill what is still missing. Use this whenever an account setup starts (pull the Vault first), whenever the rep says "update the vault", "pull the vault", "what did the client upload", "add this to their vault", or pastes a Vault agent token (kv_…), and right after reading a brainstorming call transcript for a client who has a Vault link.
---

# Kraya Vault for the account agent

The Vault is one private page per client (`vault.kraya-ai.com/<slug>`) with 15 sections. The client fills it; you read it, and you pre-fill it from the brainstorming call so the client never repeats what they already said. Everything you write there is **visible to the client** with the label "From your call on <date>" or "Kraya team".

## Access

- The token is per client and is never in the environment. Ask the rep to paste the client's **Vault agent token** (`kv_…`, minted from the Ops CRM client modal → "Kraya Vault" → Generate token). Keep it only in this session and pass it inline on every command: `VAULT_TOKEN=<token> python3 scripts/vault.py <cmd>`. Never write it to a file, a commit, a log, or a reply.
- No token → no Vault access. Don't guess a token or reuse one from another client. Tokens expire 90 days after they were minted and die when the rep regenerates one; a `401` from the script means "ask the rep for a fresh token", not "retry".
- A Vault the ops team has paused (client can't open the link) is still readable and writable for you.
- `VAULT_URL` defaults to production; set it only when the rep says to use a staging Vault.

## Commands (`scripts/vault.py`, stdlib only)

| Command | What it does |
|---|---|
| `status` | Name, draft/submitted, per-section state, open-question count |
| `pull [--out DIR]` | Writes `export.md` (whole Vault as Markdown), `manifest.json`, and downloads every file into `DIR/files/<section>/` (default `./vault`) |
| `note --section K --origin O [--date YYYY-MM-DD] [--external-id ID] (--body T \| --file P \| stdin)` | Writes a note into section K as the agent. `--external-id` makes a re-run update the same note instead of duplicating |
| `ask --section K --text T [--external-id ID]` | Posts a question the client sees under "Questions from your team" |
| `call --title T --date YYYY-MM-DD [--url L] [--duration N] [--attendee A ...] [--summary S \| --summary-file P] [--external-id ID] [--hide-recording]` | Records the call on the client's "Your calls with us" tab |

Origins: `fireflies` (call transcript), `whatsapp` (client group), `ops_chat` (what the rep told you in chat), `other`.

## When to do what

1. **Any account setup, before the Client Profile step:** `pull`. Treat `export.md` as the client's own words (it beats the checklist where they disagree, because the client typed or dictated it and confirmed agent notes). Hand `files/` to the file-extraction subagent like any other client documents. Sendable-media descriptions in `export.md` are the descriptions to use for `sendable_files`.
2. **After a brainstorming call** (transcript from the Fireflies MCP `fireflies_get_transcript` when connected, else pasted by the rep):
   - **Record the call first**: `call --title <meeting title> --date <call date> --url <Fireflies link> --duration <minutes> --attendee <each person> --external-id <fireflies transcript id> --summary "<3 to 5 sentences on what was agreed>"`. It gives the client the provenance for every "From your call on …" note. Write the summary for the client, not for ops: what was agreed and what you still need, no internal commentary.
   - The recording link is shown to the client: these are calls they were on. Pass `--hide-recording` only for a call the client should not hear (an internal one recorded by mistake, or one the rep asks you to hide).
   - Read `references/section-map.md`. For each section the call gives concrete facts for, write **one** note with `--origin fireflies --date <call date> --external-id <fireflies transcript id>:<section>`.
   - Write facts, not summaries of the conversation: "Two branches: Andheri and Thane. Open Mon–Sat 10:00–19:00." not "The client discussed their branches." Quote the client's wording for qualification questions and never-say rules.
   - For every gap the call exposed (a price sheet never sent, hours not stated, no handoff numbers), `ask` with `--external-id <transcript id>:q:<n>`. One question per fact, in plain language the business owner can answer on a phone.
   - Never fill a gap with a guess, an industry default, or a placeholder. If it wasn't said, it goes in a question.
3. **After reading the client's WhatsApp group** (`read-whatsapp-group` skill): facts the client stated there go in with `--origin whatsapp --external-id wa:<section>`; files they shared in the group are only `[type: filename]` tags to you, so `ask` them to upload those to the Vault.
4. **Facts the rep gives you in chat** ("they told me on the phone their MOQ is 500"): `--origin ops_chat`.
5. **Before writing, `pull` (or `status`)** so you don't duplicate what the client already added. If the client already wrote it, don't add an agent note saying the same thing.

## Rules for what you write

- Plain text, ≤ ~300 words per note, no Markdown headings, no bullets nested three deep. The client reads it on a phone.
- Client-facing only. Nothing about pack, credits, billing, KPIs, other clients, internal ops chatter, or the rep's opinions. See "Not for the Vault" in the section map.
- One note per section per source. Re-running with the same `--external-id` updates it; don't create "note 2".
- Do not write into `website`, `brochures` or `media` from a transcript — those are the client's uploads. A list of URLs the rep pasted is the one exception (a `website` note).
- After writing, tell the rep in one short list what you wrote (section → one line) and which questions you posted, so they can nudge the client in the group.

## Reading what comes back

- `export.md` opens with a `## Calls` block (title, date, duration, attendees, the recording link and whether the client can see it, then the summary) when calls have been recorded.
- `export.md` marks each entry with its source: `(client, …)`, `(agent via fireflies, 12 Sep 2026, confirmed by client)`, `(Kraya team, …)`. A client edit of your note shows the original underneath; the edited text wins.
- Questions appear at the end of `export.md` as `[open]` or `[answered <date>]` with the answer. Treat answers as client-confirmed facts.
- Download links in `export.md` and `manifest.json` expire after one hour; re-run `pull` rather than saving the links.
- `files/<section>/` holds the client's uploads. PDFs, DOCX and spreadsheets go to the extraction subagent like any client document. Images: open them with your file-reading tool and describe what they show when the client's own description is thin; those descriptions become `sendable_files[].description`. Audio (`dictation-*.audio`) is already transcribed into the note text in `export.md`; a note that reads "(dictation without transcript)" means transcription failed, so ask the rep to listen to the audio or ask the client to type it. Videos can't be read; rely on the client's description.
- Never push Vault content into Kraya blindly. It is input for the Client Profile and the spec; the same rules (facts only, gaps as questions, publish gate) apply.
