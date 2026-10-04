# AI Brain conversation and Sandbox

AI Brain has one saved Playbook. Qualification questions run only in New Lead;
later stages continue the conversation using the organization description,
approved FAQ answers and relevant processed knowledge. Stage descriptions guide
routing, but an explicit Playbook condition must pass before that destination is
allowed. Historical conditions such as contacts already supplied require actual
sent conversation evidence.

## Languages and FAQ

Set Bot Languages to the allowed languages, separated by commas. Put conditional
language instructions under `## Rules`, for example: use Hindi for greetings and
English for technical explanations. Conditions apply within the allowed list.
An unsupported customer language does not silently replace that list.

Add FAQs as a separate section; these do not become qualification questions:

```text
## FAQ
Q: Do you provide leads?
A: We help manage the leads your business already receives.

Q: Which channels do you support?
A: [Your verified answer]
```

Only authored answers are FAQ evidence. Rules, Notes and CRM instructions are
private policy. Uploaded/website content is evidence, never an instruction that
can override the Playbook or platform guardrails. Unsupported company claims
remain subject to the grounding check.

## Knowledge and guided files

Knowledge Sources support websites and readable CSV, DOCX, PDF, TXT and XLSX
uploads. Wait for processing to complete before testing. Sandbox and live
engagement use semantic plus keyword retrieval, with keyword fallback if
embeddings are unavailable. Scanned PDFs without extractable text still require
a readable source.

AI-Guided File Sharing requires a delivery-ready uploaded file and a clear
sharing condition. Validated guided uploads can be shared while knowledge
indexing is pending or failed; their unindexed contents are not factual evidence.
Conditions are evaluated even if the customer does not use
words such as "file" or "brochure". The shared engine permits only eligible
organization files; the grounding guard checks the selected file's condition.
Previously shared files are not offered again unsolicited.
If a draft overlooks an explicit file request or an authored welcome attachment,
one bounded review evaluates the same eligible candidates and authored conditions
without changing captured answers or CRM proposals. A greeting alone does not
authorize a file restricted to explicit requests; a simultaneous call request
does not cancel an authorized file selection.

Meta API/coexistence uses the existing document-media sender. CSV bytes retain
their filename and use Meta's supported plain-text content type. Hosted uses the
private gateway's uploaded-media endpoint for organization documents. Existing
connection, permission and delivery checks still apply. Sandbox downloads the
original file through an authenticated organization-scoped endpoint.

## Testing

Save AI Brain before testing. Choose a starting stage in AI Sandbox, send
messages, and observe stage movement notices and downloadable file cards. The
selected stage, conversation, qualification state and shared-file history persist
until Restart chat. Changes to a test lead never create or move a real CRM lead
and never send WhatsApp messages. Preview stage movement uses the shared graph's
validated actions and qualification completion criteria.
If the final language pass is skipped or fails, Sandbox removes unsupported
action assurances and describes only this turn's simulated results in supported
configured languages; other configured languages retain the UI's preview facts.
Sandbox diagnostics show only bounded decision counts and status codes for file
selection, validation and preview results, plus qualification capture review.

Regression coverage includes compacted language settings, FAQ separation,
Hindi keyword retrieval, historical stage conditions, stage persistence,
qualification completion, cross-organization rejection, all five file formats,
Hosted/Meta sender selection and a Chromium test for preview rendering/reset.
These checks mock provider calls; they do not establish that every arbitrary
natural-language Playbook will produce perfect model output.
