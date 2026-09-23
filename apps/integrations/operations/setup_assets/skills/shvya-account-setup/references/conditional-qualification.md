# Conditional qualification and final AI Playbook

Shvya AI Brain stores the organization operating specification in `ai_playbook`. Use these exact top-level headings:

1. `## Rules`
2. `## Welcome Message`
3. `## Qualification Questions`
4. `## Acknowledgment Message`
5. `## Qualification Criteria`
6. `## Stage shifting logic`
7. `## Attribute mapping logic`
8. `## Reminder creation logic`

Do not create a second `qualification_requirements` write API or place a catalogue in the questions section. Unknown headings become general rules in the parser, which can silently move content out of its intended authority section. Only the question section creates the questionnaire. Keep metadata, builder notes and source citations outside customer-visible copy.

## Structured representation

Use this tool-compatible **planning** shape, substituting actual requirement text, values, returned attribute keys and the target stage ID:

```json
{
  "mode": "configured",
  "requirements": [
    {"stable_id":"shvya_interest","question":"Which service are you interested in?","required":true,"options":[{"key":"A","value":"Consultation"},{"key":"B","value":"Product information"}]},
    {"stable_id":"shvya_consultation_time","question":"When would you prefer a consultation?","required":true,"eligible_when":{"requirement_id":"shvya_interest","operator":"eq","value":"Consultation"}},
    {"stable_id":"shvya_product","question":"Which product would you like details about?","required":true,"eligible_when":{"requirement_id":"shvya_interest","operator":"eq","value":"Product information"}},
    {"stable_id":"shvya_notes","question":"Is there anything else you would like the team to know?","required":false}
  ],
  "criteria":["All required qualification questions are answered"],
  "mappings":{"shvya_interest":["{{SHVYA_INTEREST_ATTRIBUTE_KEY}}"],"shvya_consultation_time":["{{SHVYA_PREFERRED_TIME_ATTRIBUTE_KEY}}"],"shvya_product":["{{SHVYA_PRODUCT_ATTRIBUTE_KEY}}"]},
  "target_stage_id":"{{SHVYA_QUALIFIED_STAGE_ID}}",
  "final_ack":"Thank you. Our team will review your details and help with the next step."
}
```

This is an illustration, not a claim about the onboarded company's offerings. Stable IDs are explicit, unique, persistent across wording edits and restricted to letters, digits, underscore or hyphen (maximum 64). Requirements: 1–30; question: 1–2000 characters; options: at most 30, unique keys up to 10 characters, values up to 500. Keep structured question text on one line: the inspected renderer uses its first line. Put options in their array rather than a multiline question body.

Conditions currently support equality only and must point to an **earlier** stable ID. Reject self-reference, forward reference, cycles, unsupported operators and vague labels. Compare to the canonical option value, then validate against actual compilation. A required branch question counts only when eligible. Optional questions cannot delay a route that has met its required gate. Use `mode:"configured"` when required/optional distinctions matter; do not choose majority as a shortcut around the business's hard gate.

`mappings` should use an object from stable requirement ID to an array of exact returned attribute keys. The current declared union also permits an array of strings but the inspected implementation expects mapping objects, so the object form avoids that mismatch. Every mapped attribute must exist, be active, tenant-owned and non-sensitive. Map names in final Playbook prose to these same definitions.

## Native question syntax

The structured compiler writes numbered lines like:

```text
1. [id: shvya_interest] Which service are you interested in?
   A. Consultation
   B. Product information
2. [id: shvya_consultation_time] [if: shvya_interest = Consultation] When would you prefer a consultation?
3. [id: shvya_product] [if: shvya_interest = Product information] Which product would you like details about?
4. [id: shvya_notes] Is there anything else you would like the team to know? (optional)
```

The full authored Playbook should retain this compiler-compatible question representation plus the user's requested Rules, welcome, acknowledgment, qualification criteria and explicit CRM/reminder logic. See the qualification builder and kit Playbook template for that full artifact.

## Validation and safe save

`validate_qualification_configuration({data})` validates without saving, but checks real tenant stage/attributes and compiles a structured reconstruction. It does not validate every sentence of a separately authored final Playbook. In package mode report unresolved live bindings as pending, not passed.

Use `update_ai_configuration({changes:{ai_playbook: finalText, about, bot_languages},dry_run:true,reason})` for the final complete Playbook; apply through its approval flow only when authorized. Read back full text, then run `get_qualification_configuration`, `test_ai_response_policy` and branch simulations against the stored result. If deliberately using `upsert_qualification_configuration`, review its replacement semantics first; do not run it after final authoring as an automatic cleanup step.

## Evidence and conversation behavior

- Capture explicit answers from the current conversation and permitted persisted state. Do not infer unprovided quantities, budget, consent, identity, booking or payment.
- A lead may answer several questions at once; retain all explicit facts and ask the next eligible unanswered question only.
- Letter/number options apply to the active menu. Free text maps only when unambiguous; clarify uncertainty once. A stale answer to a prior branch is not proof for the current branch.
- When an upstream answer changes, reassess eligibility; do not use old branch evidence to satisfy a new branch.
- Respect refusal without repeating the same question. A required hard gate remains unsatisfied and can route to human review; do not store a refusal sentinel as a valid quantity or completed requirement.
- Answer factual questions from approved knowledge within pricing/other disclosure policy, then resume. Asking for help or a callback is not proof of qualification, and saying thanks is not proof of purchase intent.
- A photo or payment screenshot is not verified payment. Require backend-supported evidence/human confirmation where the business process needs it.
- Completion acknowledgment is sent only through the backend completion contract; do not promise success before required persistence, stage transition and deduplication succeed.

## Acceptance cases

Test each branch fully, all required fields missing, only optional fields supplied, explicit refusal, ambiguous menu response, out-of-order answers, two answers in one turn, upstream answer correction, unsupported claim, human handoff and opt-out. Test quantity boundaries using numeric values if a verified business gate exists. Keep qualified, completed, payment verified, booked and won distinct. Record the exact tool inputs/results and limits of deterministic simulations.
