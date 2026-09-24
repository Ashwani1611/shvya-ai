# Cadence timing, channels and automation chains

## Scheduling shapes

Shvya MCP accepts `data.schedule`, not legacy `message_delay` or `after_x_units` objects:

```json
{"type":"immediate"}
{"type":"delay","delay_value":2,"delay_unit":"days"}
{"type":"specific_time","time":"10:30","weekday":1}
{"type":"recurring","recurring_every":2,"recurring_unit":"days"}
{"type":"recurring","time":"10:30","weekdays":[1,3,5]}
```

`weekday` uses Sunday=0 through Saturday=6. Units for delays are minutes/hours/days; delay must be at least 1. Recurring intervals accept hours/days, or selected weekdays plus time. Use recurring steps only for an explicitly intended ongoing schedule and verify completion/exit behavior; they are not an ordinary finite nurture substitute. Omit irrelevant fields.

Ordinary steps anchor to the sequence reference/previous completed step, with organization timezone and business-hours adjustments. A plan of Day 0, Day 1, Day 3, Day 5 becomes immediate, +1 day, +2 days, +2 days; encoding +0,+1,+3,+5 produces the wrong span. Retain both desired cumulative offsets and relative delays so they can be compared.

Run `simulate_cadence` using a known `reference_at`, inspect every due time and full duration, and test a reference outside business hours. Delivery delays can shift later steps. Business-hours settings and account health remain backend-owned. Do not label a Cadence "5 days" until its calculated schedule matches the intended meaning.

The exposed Cadence schema has no `before_x_units`, `booked_slot` anchor, arbitrary date offset or calendar reminder-sequence creator. For appointments, capture an actual confirmed datetime and use a validated Workflow date-attribute schedule only if it meets the requested timing. A fixed clock schedule is not "two hours before an appointment". Exact pre-appointment offsets require supported fields/mechanisms or a deferred integration task.

## Provider boundaries

| Need | Supported path and prerequisite |
|---|---|
| Hosted text/media | Explicit `provider:"hosted"` Cadence and a connected tenant-owned Hosted/Coexistence sender; use `add_hosted_whatsapp_step` |
| API WhatsApp | Explicit `provider:"api"` and matching connected account; `add_cadence_step` with `type:"whatsapp"` and an APPROVED template belonging to that sender |
| Email | `add_cadence_step` with `type:"email"`, subject and body; verify actual email transport readiness |
| Internal reminder | `add_cadence_step` with `type:"reminder"` and text; no customer message implied |
| Free-text Workflow WhatsApp | Canonical `message` action; account must match current lead pipeline, transport readiness checked at delivery, API service-window restriction applies |

Do not copy `extension`, `whatsapp_api`, `whatsapp_template` or `ai_call` as Shvya step/provider enums. Hosted health protections remain in force even though Meta's free-text service window is an API-specific restriction. Do not turn an API template into free-form text merely because a template creation tool is absent.

Existing Cadence sender/provider cannot be changed in place. A new provider or sender needs a new Cadence and reviewed dependency migration. Always create a Cadence with `data.is_active: true`, then append its steps and verify it remains active on readback. Isolate unfinished Cadences from enrollment and enabled Workflows; active creation alone does not authorize sends or enrollment. Add sequentially, keep returned step IDs, use the reorder tool's exact schema, and verify unique contiguous order. Edits preserve untouched content/schedule and existing active status unless a status change is requested; deletion fails if delivery history exists.

## Entry, stop and re-entry design

Every Cadence has one defined audience, entry event, eligibility condition, stop events, expiry and re-entry policy. Suggested patterns are conditional, not mandatory:

| Purpose | Start | Stop/exit |
|---|---|---|
| No-response recovery | silence threshold in chosen stages -> No Response -> recovery Cadence | reply, opt-out, human ownership, closure; on completion park in a distinct dormant state |
| Interested nurture | verified interest plus appropriate stage | reply requiring human response, booking, opt-out or closure |
| Validation/information | agreed need for a real document/process explanation | requested next step completed or human handoff |
| Appointment preparation | confirmed appointment with supported timing | cancel, reschedule, attendance or opt-out as applicable |
| No-show recovery | human/system-confirmed non-attendance | rebook, decline, opt-out; do not assume silence means no-show |
| Dormant revival | permitted recontact and an actual new reason | any opt-out; reply routes through requalification policy |
| Post-purchase | verified won/order state and authorized service messages | completion or preference restrictions |

A lost/opted-out stage must not automatically feed a marketing revival. Keep `Lost`, `Dormant`, `Not Eligible`, `Human Intervention` and `Do Not Contact` meanings distinct. Re-entry requires the business's consent and freshness policy, not a generic wildcard that re-enables everyone.

## Workflow interlock checks

- Build exact event/condition/action rows from the live schema. Avoid direct intent-keyword -> Qualified rules; qualification requires its own evidence contract.
- STOP suppression needs protection from all restarting paths, queued delivery and bump-ups, not only the current Cadence. Validate applicable stage/source scopes and normalized variants.
- Terminal stages stop irrelevant sales Cadences. A deliberate post-purchase Cadence must have separate eligibility and content.
- Handoff stops qualification/nurture and switches AI/follow-up according to the actual human-owned stage policy. Do not continue qualification after a request that requires stopping it.
- Reply-stop (`*`) and stage-start rules can overlap. Simulate interactions and ensure reply-stop cannot be undone by a later matching start.
- Silence -> No Response -> recovery -> Dormant must terminate. Never complete a recovery Cadence into its own entry stage or cycle between two Cadences.
- Compare all timing sources: Cadences, Workflows, AI bump-ups, account auto-follow-up and business hours. Choose an owner for each nudge to avoid duplicate contact.
- A no-answer phone call, unanswered WhatsApp message and inactive stage are different events. Do not replace one with another based solely on an industry template.
