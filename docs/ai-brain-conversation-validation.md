# AI Brain conversation validation

AI Brain has two separate responsibilities: retrieve approved company facts and
execute validated Playbook actions. A successful generated sentence is not proof
that a file was delivered, an attribute saved, a reminder created or a stage moved.

## Regression coverage

The conversation fixes cover these failure paths:

- A long previous assistant answer must not remove the latest customer question
  from the bounded retrieval query.
- Company About text, active FAQ answers and current indexed source passages must
  remain available to the independent grounding check.
- Stage and attribute rules must resolve supplied CRM entities and explicit
  customer evidence, including Instagram conversation history.
- Knowledge upload replacements must retain their reserved version when worker
  execution is delayed or reordered.
- Guided file selection must be consumed by the active transport.
- Sandbox must preserve selected acquisition source and simulated channel, and
  label action results as previews.
- Short multilingual questions and accepted offers (for example, "yes" after an
  offer to explain pricing) must still retrieve knowledge.
- Accepted qualification answers must not skip configured language, Playbook,
  attribute or guided-file decisions merely because their extraction was cheap.
- Generation and independent grounding must see the same committed reminders
  and source-bound action results.
- Unqualified completion stage names bind to the lead's current pipeline;
  explicitly named destination pipelines must resolve exactly.

## Staging acceptance scenarios

Use an isolated test organization with approved test files and internal recipients.
Never use a production lead to exercise stage/reminder or outbound-message tests.

| Scenario | Expected result |
| --- | --- |
| Ask what the company does with only About populated | An answer supported by About in a configured language |
| Ask a differently worded FAQ question | Relevant answer with no unrelated promises |
| Ask a fact located in an uploaded document | Answer supported by the current active document version |
| Ask a fact located at a configured public URL | Extracted and indexed page/document content is used |
| Replace the same source twice; process the older job last | The newer source version remains authoritative |
| Answer each configured qualification question | Each answer is saved once; answered questions are not repeated |
| Meet explicit qualification criteria | Exactly the configured valid destination is selected |
| Fail a required qualification criterion | No unauthorized move to Qualified |
| Give a mapped option, boolean or numeric answer | Configured attribute receives a validated typed value |
| Request a callback with a clear time/timezone | One reminder is recorded, including on event retry |
| Request a callback with an ambiguous time | Clarify the missing time rather than invent a precise appointment |
| Meet a guided file's sharing condition on WhatsApp | Selected organization-owned file is queued through the bound account |
| Meet the same condition on Instagram | An expiring, authorized download link is queued in the same conversation |
| Test Instagram-specific Playbook rules in Sandbox | Selected acquisition source/channel reaches the prompt; effects remain previews |
| Ask about a fact absent from every approved source | Explain the specific missing detail without inventing facts |

Inspect the AI Trace and persisted CRM/outbound state for these scenarios.
Automated provider mocks check contracts and regressions; they do not establish
real-model accuracy or provider delivery. Test a representative set of real
questions after deployment before claiming end-to-end completion.

## Source limits

A configured URL is an ingestion source, not a promise of live web browsing on
every reply or recursive crawling of every linked page. Extracted text must be
available and indexed before it can ground an answer. Scanned/image-only documents
still need a supported OCR pipeline or a text-bearing replacement.

When lexical FAQ matching yields no answer, the runtime can supply a bounded set
of complete, active organization FAQ question/answer pairs to handle differently
worded or multilingual requests. These candidates require independent relevance
verification; appearing in the context never proves a FAQ answers the question.
This is bounded context, not a semantic index of every FAQ. Oversized pairs are
omitted rather than truncating conditions or exceptions.

Instagram sends arbitrary guided documents as signed download links, not native
document attachments. Delivery rechecks the document's eligibility; retiring a
file or removing its sharing guidance revokes access.

Provider failures can still require a safe fallback. Inspect the trace's
grounding validation reason to distinguish missing evidence from a verifier
timeout or provider error. Passing mocked regressions does not measure real-model
answer quality or confirm deployment to either environment.
