# Intake sections and Shvya destinations

Destination names below are real exposed MCP capabilities, not automatic mappings. Confirm current allowed capabilities before proposing writes.

| Key | Capture | Candidate destination / limit |
|---|---|---|
| `website` | User-supplied canonical website pages and purpose | `create_knowledge_source` proposal using its discovered schema; never crawl unrelated links automatically |
| `brochures` | Supplied catalogs, documents and price sheets, source version | `upload_knowledge_document`; `get_knowledge_health` exposes metadata, not file text |
| `media` | Authorized local media, observed description, intended use | Attachment inventory; current MCP has no general sendable-media library equivalent |
| `offerings` | Services/products, units, prices, disclosure policy | About through `update_ai_configuration`; factual FAQs through `upsert_faq`; disclosure behavior in AI Playbook |
| `basics` | Branches, address, phone/email, hours and timezone, verified booking/payment links | About/FAQs; supported WhatsApp hours through `update_messaging_automation_settings` |
| `faqs` | Real questions, objections and authorized answers | `upsert_faq`; concise reusable copy may also become a `upsert_touchpoint` draft |
| `team` | Roles, ownership and escalation order; necessary business contacts | AI Playbook and validated Workflow design; no invented user IDs or assignment tool |
| `qualification` | Definition, disqualifiers, exact questions/options, conditional applicability | `validate_qualification_configuration`, then proposed `upsert_qualification_configuration`; custom attributes use `upsert_attribute_configuration` |
| `handoff` | Human-request, complaint, readiness, custom-quote triggers and acknowledgment | AI Playbook, real stage AI switches and validated Workflows; do not promise a callback without a working process |
| `blacklist` | Client-specific prohibited topics, restricted pricing and disclosure rules | AI Playbook behavior; applicable backend policy remains authoritative |
| `rules` | Persona, tone, language/script, emoji, never-promise rules | `update_ai_configuration` proposal for Playbook and supported languages |
| `proof` | Approved quotes, attribution, metrics, units, evidence/permission | Facts/FAQs or approved knowledge; do not turn old claims into guarantees |
| `offers` | Offer eligibility, start/end date and timezone | Factual FAQ/About proposals; expired offers must not remain active copy |
| `scripts` | Supplied historical calls/chats/email examples | Tone and flow evidence only; historical customer claims are not business policy |
| `other` | Unclassified facts and questions | Triage before producing configuration |

Calls commonly supply qualification, handoff, rules and restrictions; business basics and offerings are often partial. Preserve precise wording where required. Calls mentioning media do not provide the media; record the missing artifact. Supplied URLs can be listed under `website`; do not invent URL slugs.

Client-facing exports exclude secrets, customer personal records beyond necessary approved contacts, billing/credit commentary, staff opinions and other organizations. Internal delivery, integration, commercial and KPI notes belong in a separate restricted project artifact, not the intake export. This separation does not create a new Shvya portal or change visibility settings.
