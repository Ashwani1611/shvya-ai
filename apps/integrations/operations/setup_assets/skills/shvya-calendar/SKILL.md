---
name: shvya-calendar
description: Configure, validate and troubleshoot Shvya Calendar booking pages, availability, provider sync, reminders and booking outcomes.
---

# Shvya Calendar

Own booking configuration and booking-state operations exposed through Operations MCP.

## Native MCP first: complete page setup

The dashboard and Windows computer-control service are **not prerequisites** for booking-page creation. For a selected tenant, use the live tool catalog and verify that `calendar.config.write` is **effective**, not merely allowed by policy. When missing, report the precise missing OAuth grant and request fresh authorization; never claim that a browser connection is required. An older deployed MCP may still lack tools even when this skill documents them.

1. Call `get_capability_discovery`, then `get_calendar_configuration`. Select the correct organization explicitly and check effective permissions.
2. If no page exists, call `create_calendar_page` (name, optional slug and type, reason, dry_run). Inspect the proposal/required approval, execute only when authorized, then read back the created draft page ID.
3. Use `upsert_calendar_configuration` to set name, branding (`logo_url` must be an existing HTTPS image asset; direct binary uploads are not supported here), accent color, timezone, availability, duration, notice, capacity, CRM pipeline/stage and host. Slug changes require a draft/unpublished page. Check that IDs belong to the selected tenant. Set `status=published` only after validating required fields and approval.
4. For each configured email/WhatsApp reminder, use `upsert_calendar_reminder`; a reminder definition does **not** prove transport delivery. Read back configuration and inspect `validate_calendar_configuration` warnings after authoring.
5. For a requested date, call `get_calendar_available_slots` to check canonical capacity, notice, blocks, availability and connected Google busy time. Call `get_calendar_setup_readiness` to report actionable setup gaps, without implying real booking success.
6. Return the actual `public_url` from `get_calendar_configuration` or the mutation response. Do not guess a URL from a slug. This is URL construction, not an HTTP availability test.
7. Use `inspect_calendar_public_link` for route/publication diagnostics; this is **not** an external HTTP probe. Use `probe_calendar_public_https` only for approved SHVYA-hosted URLs to check HTTPS reachability; it cannot prove a booking, slot delivery, or reminder receipt. For existing bookings use `verify_booking` and `get_calendar_delivery_evidence` for booking status, provider synchronization and recorded reminder-delivery state. Do **not** describe configuration validation as a completed real-world acceptance booking. A real customer booking and provider delivery need separate authorized end-to-end testing and evidence.
8. If no Google Calendar connection is configured, state that external provider busy-time synchronization cannot be verified; native SHVYA availability can still be configured. Do not invent connected accounts or claim provider delivery.

## Workflow

1. Verify tenant and read `get_calendar_configuration` plus relevant integration lifecycle/health.
2. Validate page publication, opening hours, duration/capacity, buffers, provider token presence and reminder coverage with `validate_calendar_configuration`.
3. Apply safe page changes through `upsert_calendar_configuration` using dry-run/approval/read-back.
4. For one booking use `verify_booking` to inspect canonical state, provider sync and reminder delivery.
5. Use `reschedule_booking` only after canonical availability validation. Use `update_booking_status` for supported outcomes: cancelled, completed or no_show.
6. Confirm cancellation suppresses pending reminders and verify provider synchronization after state changes.
7. Separate configuration readiness from a real booking acceptance test.

## Guardrails

Do not invent slots from a diary or external calendar not connected to the organization. Do not mark a booking confirmed merely because a requested time is syntactically valid.

## Output

Return page/booking state, validation findings, provider sync, reminder state, applied changes and acceptance checks.

## Shared quality contract

Before material live work, read the shared [skill quality contract](../../framework/skill-quality-contract.md), [evidence and attribution model](../../framework/evidence-and-attribution.md), [known-trap method](../../framework/known-trap-method.md), [execution and recovery contract](../../framework/execution-and-recovery.md), and this skill's [domain checks](references/domain-checks.md). Load only what the task needs, but do not report a defect before applying the relevant trap check.

Treat `evals/evals.json` as behavioral acceptance rubrics, not executed test evidence. A successful tool response is never sufficient on its own: verify authoritative read-back and the requested business effect at the strongest evidence level available. Keep UNKNOWN, conflicting and unavailable evidence explicit instead of guessing.
