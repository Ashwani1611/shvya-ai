# Demonstration script authoring

This is a presenter guide, not test execution authority. Use only disposable harness fixtures or exact separately authorized test recipients. Generating it sends nothing, changes no CRM state and connects no provider.

## Scenario structure

For each scenario specify title, business purpose, fixture label, required initial pipeline/stage/AI state, dependencies, and whether it is script-only, a documented existing engine result, or a documented existing live delivery result. Do not inherit reference-company dates, phones, budgets or products.

Each step must contain:

| Field | Content |
|---|---|
| Send / Do / Explain only | One concrete input or action for the approved target; timer walkthroughs are explain-only. |
| Expect | Faithful paraphrase of configured behavior, not an invented exact AI reply. Describe silence if effective gates block a reply. |
| Dashboard | Exact observable stage, attribute, assignment, reminder or Cadence change; include expected absence where meaningful. |
| Why | How this follows from the account's rules. |
| Say | A short client-facing presenter line; no unsourced response-time or reliability promises. |
| Evidence | Source IDs for the rules and settings; execution result ID only when it exists. |

## Before the demonstration

1. Inspect sample-data and pending-message risks without deleting or exporting anything automatically.
2. Verify actual connection, pipeline-bound sender, stage and lead gates, credits and sending window. Connection steps require separate authority.
3. Identify the fixtures and their starting states. Do not select random customer numbers.
4. Explain channel-specific reset/cleanup limitations using verified current behavior, not historical provider assumptions.
5. Set the running order. Reusing a fixture across scenarios requires its previous state to match the next scenario.
6. State what will only be narrated: multi-hour timers, inactive paths, disconnected channels and unsafe effects.

## Applicable scenario coverage

- First enquiry and grounded welcome once.
- Answer extraction and the next unanswered eligible question.
- A product/pricing/FAQ interruption before qualification continues.
- Compound answers and inferred values only where actually configured.
- Completion criteria, acknowledgement and backend-allowed transition.
- Later-stage support without silently restarting intake.
- Alternative timing, segment or language branch.
- Not-a-fit routing, including any unintended future win-back.
- Human request and the actual alert/suppression behavior.
- Opt-out across current AND future automation paths.
- Actual approved file sharing, not merely an indexed document.
- Every material Cadence: how it starts, ordered steps, relative/cumulative timing, stop, replacement and end.
- Calendar, catalog or voice only when configured and safe; otherwise explain omission.

Do not wait out long delays, shorten production delays or manually assign immediate messages to a human-owned/quiet stage to make the demonstration look active. A provider-accepted message is not confirmed delivery.

## After the demonstration

Describe scoped fixture cleanup/resumption through the existing authorized harness. Never instruct blanket lead deletion or organization resets. Record requested changes for separate setup work instead of rebuilding during the call. Distinguish intended cleanup from confirmed completed cleanup.

## Presentation quality gate

No generic "test the chatbot" instructions. Each scenario must let another onboarder run the call without guessing what to type, what the card should show or how to explain it. Avoid unsupported "within a minute" claims. Frame configured behavior as expectations and surface blockers directly in the relevant steps.
