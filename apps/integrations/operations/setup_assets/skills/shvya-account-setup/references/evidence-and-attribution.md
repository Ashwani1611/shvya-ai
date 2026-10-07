# Evidence and attribution model

Use this model to decide what a source can prove. Evidence strength depends on the question; no single source is universally strongest.

## Evidence ledger

For every material finding track:
- organization and resource/lead/message ID;
- source/tool;
- captured time and business-event time;
- exact claim;
- coverage window / truncation / redaction;
- confidence;
- conflicting source, if any;
- producing layer.

## Source classes

| Source | Can prove | Cannot by itself prove |
|---|---|---|
| Canonical persisted state | current configured/business state | that an external side effect occurred |
| Provider-confirmed state | provider acceptance/status | that SHVYA intended the correct business action |
| Runtime/production trace | what path executed and bounded rendered evidence | all historic behavior outside trace coverage |
| Actual conversation / booking / delivery record | observed customer-facing behavior | why the producing layer chose it without further trace/config evidence |
| Configured Playbook/Workflow/Cadence | intended rules | that they triggered or executed |
| Knowledge/FAQ content | approved available facts | that retrieval selected them on a particular turn |
| Intake/call/group evidence | what a source reported/requested | that it was implemented |
| Operator/user description | task intent and reported symptom | technical root cause |

## Attribution rules

1. Search configuration/knowledge before calling a wrong factual answer a model hallucination.
2. Trace the event path before accepting the reporter's diagnosis of the failing subsystem.
3. Two summaries derived from the same original source are not independent corroboration.
4. Current state and incident-time state may differ. Date every relevant configuration or trace finding.
5. A bounded sample supports only a bounded claim. Never project a sample count to the full organization without evidence.
6. When two authoritative sources disagree, report the conflict and resolve from the owning system or explicit business confirmation; do not average.
