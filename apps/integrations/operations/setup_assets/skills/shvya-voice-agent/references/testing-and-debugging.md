# Voice testing and debugging

Template rendering is deterministic and does not test a live voice provider. When a voice adapter is implemented, authorized end-to-end testing must exercise the same context injection and backend handlers as production. A provider dashboard test with absent instructions or placeholder lead data cannot validate the Shvya flow.

Before a live test, verify an approved isolated test recipient, consent/timing checks, safe synthetic lead context, the expected published version and terminal function. Do not clear or mutate a real customer's attributes to make a test pass. Change one behavior at a time and record the prompt/flow hash, adapter version, provider version, test scenario and outcome. Verify effective state by re-reading and through a call, not merely a successful write response. Retain reviewed prior artifacts for rollback.

Required scenarios:

| Scenario | Observable expectation |
|---|---|
| Missing/placeholder context | Does not say `na`, an example name or a template token; asks only necessary unanswered questions |
| Prefilled reliable answers | Skips repeat questions and evaluates remaining canonical requirements |
| Contradictory or corrected answer | Preserves evidence, clarifies and lets backend recompute; never rigidly clings to an earlier wrong classification |
| Conditional no | Skips dependent questions; does not repeatedly reconfirm |
| Language switch / proper noun / amount | Supported language remains coherent; pronunciation checked by listening, not only transcript spelling |
| Short answer / STT miss | Clear synonyms accepted; material uncertainty clarified once and recorded |
| Restricted price / unsupported service | Uses approved disclosure response, no fabricated figure or promise |
| Booking interest | Records request; no slot/payment/send claim without corresponding backend success |
| Caller opt-out or human request | Sales flow stops; verified suppression/handoff result is recorded, including failure handling |
| Call objective reached | One recap, one close, terminal action; no repeated payment/link/name loop |
| Silence / no progress | Bounded provider timeout and terminal close |
| Duplicate completion callback | Exactly one effective result through canonical backend idempotency |
| Missing required answer | No protected Qualified transition |
| Provider tool failure | No success claim; incident and pending outcome are visible |

When recordings/transcripts are authorized and available, preserve their source, call ID, versions, timestamps and known redactions. Transcripts may describe recognized speech rather than the model's raw text; they cannot by themselves prove what text the model generated. If caller speech was missed, inspect the recording before attributing it to hallucination. Channel layout must be verified before separating stereo audio; left/right speaker assumptions are unsafe. Silence detection helps locate spans but does not establish the meaning of speech.

Measure caller-end to assistant-start only when reliable timing exists. Separate recognition, model, synthesis, transport and playback latency where traces expose them; a long gap alone does not identify a layer. Review post-call field coverage and downstream actions independently of how fluent the voice sounds. Report native call logs, recording or provider-version evidence as UNAVAILABLE when absent rather than substituting WhatsApp message traces.
