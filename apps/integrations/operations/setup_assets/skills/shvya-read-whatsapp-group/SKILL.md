---
name: shvya-read-whatsapp-group
description: Read and summarize an authorized, user-supplied WhatsApp support-group export for Shvya onboarding or review, preserving participants, dates and evidence gaps. Native Shvya lead-conversation reads are a separate supported path; group/session discovery is currently unavailable.
---

# Read WhatsApp requirements for Shvya

The current Shvya MCP has `get_conversation({lead_id, channel, limit})` for recent lead messages and `list_whatsapp_accounts` for account identity/status. Neither reads an arbitrary support group, enumerates hosted group chats, or grants access to an employee's WhatsApp session. `analyze_setup_group_export` formats supplied exports only; it has no live group-read connection. Do not fabricate a group tool, inspect credential files, call a legacy WAHA endpoint or infer access from an ops phone number.

For a support group, work from the exact authorized export supplied by the user. If none is available, finish the independent configuration work and report the group requirements as UNAVAILABLE with the needed artifact. Do not infer that there were no requests. For a real lead conversation, verify the active organization with `get_operations_context`, resolve a known tenant-owned lead ID, then use `get_conversation`; treat limits/redactions as coverage constraints. Do not substitute a lead chat for a support group.

1. Match organization, group/chat ID and participants before reading. Similar names do not establish identity; multiple plausible groups remain unresolved until selected. Never combine groups from different companies.
2. Record supplied export source, earliest/latest included timestamps, timezone, message count, gaps and whether history is complete. Use the company's confirmed timezone, not an assumed country. Phone country codes and chat IDs are not invented.
3. Read only the range needed. Preserve message IDs or source line references. Order chronologically, maintain multiline replies, and label unresolved participant roles as unknown. `fromMe` indicates the export owner's direction, not whether someone represents Shvya.
4. Extract discrete requirements, corrections, complaints, commitments and visible resolution. Quote the evidence-bearing words; distinguish customer requests from Shvya promises and later corrections. Flag unanswered questions and unresolved conflicts. A forwarded call summary is not independent corroboration of that call.
5. Media-only messages are evidence gaps until an authorized file/transcription is supplied. A filename, screenshot marker or caption does not prove the attachment's contents. Do not fetch attachment URLs, voice recordings or private links without task scope and available safe access.
6. Report the exact scope read, requirements with source references, complaints/resolution, promises/visible delivery, and coverage limits. Silence in a partial export does not prove a response delay or missing delivery. Produce artifacts only; this skill never sends messages.

Read [export-format.md](references/export-format.md) for the normalized JSON contract. Use `analyze_setup_group_export` with the supplied `data`, exact `chat_id`, explicit IANA `timezone`, optional inclusive `since`/`until` dates, and bounded `limit`. The backend validates the export organization against the active authenticated tenant, offset-aware timestamps, identical-message deduplication and conflicting duplicate rejection. Read returned `formatted_text` and coverage metadata together; a bounded excerpt is not a full-history finding.

This tool accepts supplied text only. It does not discover employee sessions, connect WhatsApp, download attachments, read arbitrary live groups or send messages. Normalize raw text using its known date convention before submission; preserve uncertainty instead of guessing day/month order. No client scripts or credentials are required.
