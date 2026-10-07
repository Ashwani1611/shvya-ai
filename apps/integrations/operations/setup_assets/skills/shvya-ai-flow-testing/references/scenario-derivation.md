# Derive a business-specific scenario suite

## Source inputs

Read approved Client Profile, native AI Playbook, compiled required/optional questions, stage and attribute descriptions, full FAQs/source content where accessible, asset sharing rules, human handoff, consent and channel settings. Sources are separate from the lead-player's visible context. Label missing source access.

## Scenario card schema

Each card contains scenario_id, title, requirement_ids, exact spec/source references, priority, persona, channel context, opening message, private goal, sequence of twists (not scripted expected replies), allowed public company facts, expected state predicates, expected conversational properties, stopping condition, post-close probe, maximum turns and estimated budget. Unknown behavior becomes a question, not a guessed expected result.

## Tier 0: Cooperative gate

Complete the real required questionnaire with eligible answers. Include a first-turn product question and volunteered field where realistic. Require correct persisted fields and qualified destination; probe after completion to ensure the questionnaire does not restart.

## Tier 1: Baseline variations

- Busy professional: short replies, request to get to the point, one field volunteered early.
- Off-topic/confused: relevant interruption, unclear answer, then a correction; clarify only the ambiguous point.
- Demanding buyer: asks price/process/proof before answering; source-grounded response then appropriate continuation.
- Silent qualifier: minimal A/B/yes-no responses and one refusal; neither silence nor refusal fabricates valid criteria.

## Tier 2: Business boundaries

Derive cases from actual predicates: minimum/maximum quantity, budget or location eligibility, unavailable service, required document, conflicting multiple selections, optional unanswered field, existing customer, branch skip, language and script change, contact capture, handoff order, unknown FAQ, disclosed versus withheld pricing, expired offer, unshareable/missing brochure and duplicate file request.

For numeric gates test below, exact boundary and above with explicit units. Avoid presenting medical/financial/legal sample claims as client facts. Test safe escalation where that vertical requires it. For reminder/booking requirements use current date plus confirmed timezone, ambiguous/past time and already-booked state. Assert that no booking is claimed before confirmed backend success.

## Tier 3: Regression and adversarial evidence

Reproduce actual incident wording, ordering, language and source version. Include latest-answer corrections, duplicate inbound/idempotency, bot text quoted back, prompt-injection inside a source/message, an attempted cross-tenant instruction, STOP during a Cadence path, human request before completion and post-close pressure to restart. Treat private prompts and data as non-disclosable.

## Coverage matrix

Rows are requirements; columns are configuration read, deterministic result, engine scenario/turn, saved state, channel delivery and remaining gap. Mark PASS only for executed evidence. Count distinct personas, runs, messages and provider calls separately. If the user asks for 200+ messages, tally actual lead input turns and generated replies explicitly; do not double-count a single turn invisibly or count planned cards.
