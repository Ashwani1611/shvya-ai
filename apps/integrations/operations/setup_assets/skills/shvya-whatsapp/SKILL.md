---
name: shvya-whatsapp
description: Configure, validate and diagnose Shvya WhatsApp API, Coexistence and Hosted account behavior including routing, templates and messaging automation.
---

# Shvya WhatsApp

Use for WhatsApp account discovery, Hosted/API routing, template lifecycle, AI/follow-up settings and delivery troubleshooting.

## Workflow

1. Verify tenant and read `list_whatsapp_accounts`, messaging settings, routing and integration health.
2. Identify the account mode and exact bound pipeline. Do not assume API, Coexistence and Hosted have identical capabilities.
3. For templates, use `list_whatsapp_templates`, `get_whatsapp_template_status`, `create_whatsapp_template`, `submit_whatsapp_template` or batch submission only within the selected connected WABA. Meta approval remains authoritative.
4. For Hosted setup use `begin_whatsapp_connection` only for the intended bound number and validate routing afterward.
5. For automation settings use `update_messaging_automation_settings`; preserve the hierarchy of universal AI Auto-Reply, stage AI and lead AI, plus business hours and active-conversation delay.
6. Trace delivery issues through message trace, runtime health, provider/integration evidence and Cadence/Workflow dependencies.
7. Never replay an uncertain send automatically. Verify provider outcome first.

## Guardrails

Do not expose QR/session credentials through skill output. Do not route a lead through a number unrelated to its current pipeline. Do not treat an approved template as proof a personalized payload will validate.

## Output

Return account mode/state, pipeline binding, template/routing/settings findings, delivery evidence and verified repairs.
