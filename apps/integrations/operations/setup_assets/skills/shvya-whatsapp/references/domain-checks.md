# WhatsApp domain checks

## Evidence
Identify account mode (Cloud API, Coexistence, Hosted), connection/lifecycle state, pipeline binding, templates/status, messaging settings, lead/channel trace, webhook/inbound evidence, outbound/delivery evidence and runtime health.

## Known traps
- API/Coexistence/Hosted are not interchangeable.
- Template approved is not payload valid; variable/media/button contracts still matter.
- CTA/postback payload can differ from displayed text.
- Hosted connection health does not prove Django callback/AI worker health.
- Historical sync is not a live inbound message and should not trigger new engagement.
- Universal AI ON is not sufficient when stage/lead AI is OFF; OFF at any required gate suppresses AI.
- Welcome/follow-up must respect business hours, delay, handoff and opt-out.
- Queue/retry attempts are not unique customer messages.

## Verification
Validate account→pipeline routing, template contract, automation settings and one representative trace from inbound to provider outcome. For connection changes, verify lifecycle/routing again.

## Handoffs
Generic topology → channel routing; Cadence → Cadence builder; AI runtime → AI debugger; lifecycle disconnect/reconnect support → integration manager.
