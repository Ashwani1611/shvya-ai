# Integration manager domain checks

## Evidence
Read capability discovery, integration lifecycle, provider/account identity, auth/subscription/health state and downstream dependencies in routing, Workflows, Cadences, Calendar and team settings.

## Known traps
- Connected, authenticated, subscribed, healthy and operational are different states.
- Disconnect support does not imply connect/reconnect support exists.
- Clearing credentials must preserve historical business records.
- Provider identity may be shared by several features; dependency impact must be inspected before disconnect.
- A stale token can look like webhook or routing failure downstream.
- Generic lifecycle status may hide provider-specific permission/review requirements.

## Verification
For lifecycle changes: inventory dependencies → dry-run → approval → apply → read-back → provider/domain validation. After disconnect ensure credentials are cleared, history remains, and dependent features are not falsely reported ready.

## Handoffs
WhatsApp/Instagram/email/Calendar specifics go to provider skill; runtime health → diagnostics/incident repair.
