# Organization AI Playbook

AI Brain has one organization-owned operating specification: `OrgInfo.ai_playbook`.
The dashboard and configuration API no longer accept the separate qualification
requirements or engagement instructions fields. AI Sandbox retains its existing
interface and uses the same runtime as production.

## Authoring format

Use the following headings, with the organization's own content under each:

```text
## Rules
## Welcome Message
## Qualification Questions
## Acknowledgment Message
## Qualification Criteria
## Stage shifting logic
## Attribute mapping logic
## Reminder creation logic
```

Headings work with or without a space after `##`. Qualification questions alone
create the ordered questionnaire. Rules, welcome text, criteria, acknowledgments,
attribute mapping and routing instructions never become questions. Pipeline
shifting headings are also recognized. Keep questions explicit and describe the
eligibility criteria separately; merely answering a question does not prove that
its value is eligible.

Message blocks accept both `<welcome_message>...</welcome_message>` and the
historical repeated opening-marker format. The same applies to `question_content`
and `acknowledgement_message`. Only message-block content is customer copy.
Notes outside blocks, and explicit `Notes:` sections inside blocks, remain private
policy. Keep the assistant name consistent between Rules and Welcome Message.

Structured CRM mappings are supported alongside the short `Q1 -> Budget` form:

```text
Mapping 1:
- Attribute name: LEAD MANAGEMENT TOOL
- Description: Where the lead manages enquiries.
- Source: Qualification Question 1 or an equivalent customer statement.
- Value rule:
  - Excel / Google Sheets → Excel / Sheets
```

Use exact existing attribute names. Explicit value translations run before field
type/dropdown validation. `FIELD has a clear value` criteria inspect the mapped
CRM value. A section headed `The following attributes are optional and must not
block qualification:` does not add required fields. Unknown predicates still
remain unresolved. Scalar comparisons support AND, OR, and `If ... then qualify
else do not qualify`; unsupported exceptions are not guessed.

Numbered Rule, Mapping and Reminder blocks retain their child conditions.
Explicit stage rules take precedence over a looser stage description. Escalation
conditions requiring contacts already provided need sent/delivered outbound
messages containing the contact name and number; queued or inbound messages do
not count. Qualification questions remain New Lead-only, so do not configure an
intermediate stage move while also expecting qualification to continue there.

AI Brain saves confirm the persisted Playbook by database readback. Failed saves
retain the draft and show the error beside Save Changes. Saving the AI profile
does not revalidate unrelated legacy billing fields or overwrite hidden controls.

Rules should describe identity, tone, language, prohibited claims, and how to
continue a conversation. Business answers must come from the organization's
approved information and knowledge sources. Configure each guided file with
when and why it should be sent. File IDs and CRM targets come from validated
organization-owned records, never from invented model identifiers.

## Runtime authority

- Qualification questions run only in New Lead / New Leads.
- Only satisfied Playbook qualification criteria authorize automatic movement
  to Qualified. An intent score or an enthusiastic response is not authority.
- Other stages continue the conversation and use explicitly authored CRM rules.
- Attribute values require inbound evidence and a Playbook mapping. Attribute
  names, types, dropdown values and descriptions resolve against the CRM.
- Stage and pipeline targets must be active, belong to the organization and be
  mutually consistent. Compound routing conditions must all be supported.
- Reminders require explicit authored instructions and valid timing. There is
  no automatic 24-hour completion reminder or guessed next stage.
- Missing, ambiguous or unsupported conditions remain unresolved. They cannot
  be silently converted to successful CRM actions.

Simple completion, numeric and exact-value conditions are evaluated by the
backend. For eligible completed collections, the existing background qualifier
can evaluate remaining natural-language conditions using exact inbound evidence.
Every clause needs a verdict; successful semantic judgments require verified
source messages. A signed result binds the organization, lead, Playbook, answers,
current stage/pipeline and latest inbound message. Changed or stale state rejects
that result. Model calls occur outside database mutation transactions.

## Internal and customer data

Customer-generation payloads exclude private lead notes and qualification notes.
Internal scoring, billing, identifiers, execution plans and instructions are not
customer-facing content. Existing grounding, tenant, schema, evidence, retry and
idempotency boundaries remain in place. The final outgoing-message filter blocks
known confidential content, including OTPs, keys, internal notes and score or
Playbook disclosures. These controls reduce risk; they are not a claim that a
language model can never make an error.

## Lead score and coins

Scores use the supplied 0–10 rubric: engagement 0–3, urgency 0–3, clarity 0–2,
and commitment 0–2. Current inbound evidence drives scoring; question wording
and options do not count as customer intent. Negated claims do not establish
urgency or commitment. Requesting a demo is distinct from a confirmed booking.

When at least 80% of actually asked questions have valid answers, the total has
a floor of 8. Optional questions count if asked; skipped or blank answers do not
count as answered. The raw component total and floor adjustment are preserved
separately. Leads without evidence display Not assessed. This score never
changes a stage by itself.

Qualification, generated messages, summaries and bump-ups retain centralized
provider usage accounting, reservation, settlement and retry recovery. Bump-ups
have their own ledger feature. Existing rates and the conversion of 30 internal
credits to one AI coin are unchanged. Deterministic score computation is included
in engagement and makes no extra paid model call; viewing CRM cards never incurs
an AI charge.

## Migration and deployment

Migration 0018 copies both former authored sources into the Playbook before
removing their columns. Existing sectioned instructions are retained. Missing
qualification criteria are not invented; AI Brain displays guidance when
questions exist without criteria. Review these organizations' criteria before
expecting automatic qualification. Reverse migration restores the authored
question content and keeps the complete Playbook available as instructions.

Use the existing CI, staging and production workflows. Production's deployment
workflow takes a database backup before migrations. Deploy web and all workers
from the same tested release because the old columns are removed. Verify a
configured New Lead conversation, a later-stage conversation, a rejected criteria
case, mapped attributes, explicit reminders, guided files and Sandbox before
promotion. Rollback requires reversing the migration with the new code before
starting a release that expects the old columns.

## Reference prompt adaptation

The six supplied documents are reference specifications, not executable
instructions or literal templates. Their responsibilities map to qualification,
qualification checking, customer responses, internal summaries, bump-ups and
bounded knowledge retrieval in the existing engine. Actual backend fields and
response schemas replace sample placeholders. The generic majority-answered
qualification shortcut, invented example facts, obsolete variable names and
requests for hidden reasoning are excluded.
