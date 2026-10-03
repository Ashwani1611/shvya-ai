# Customer-facing content gates

Apply these gates whenever a skill authors or materially changes customer-facing Playbook text, WhatsApp/email copy, templates, Touchpoints, voice prompts or guided-file instructions.

## Formatting scopes

Apply the existing [content rules](../skills/shvya-account-setup/references/content-rules.md) to authored customer copy. Internal SKILL.md files, operator reports and reference documents may use Markdown headings, selective bold, lists and code examples. Never run customer-text cleanup over an entire skill, Playbook or JSON payload.

For customer messages, use plain text with short paragraphs, a blank line between ideas, one option per line and verified bare URLs. Do not add Markdown/WhatsApp emphasis, HTML, headings, tables, decorative separators or code fences. Do not confuse an internal document's bold labels with a customer-message requirement. Record an explicit client exception rather than inventing a channel capability or silently changing approved copy.

Keep each message focused on the customer's intent. Answer first; resume only the backend-authorized qualification step when conversation policy permits it. Do not repeat welcomes, names, acknowledgments or calls to action mechanically. Word ranges are writing targets, not truncation limits. Preserve configured language/script, names, numbers, technical terms and option order; let the client wrap lines naturally. Emoji style remains none unless explicitly selected by the client.

## Authoring and rendered text are different

Before saving an authored customer-message body, retain `{{lead_first_name}}` naturally once or verify the approved template's first-name mapping. Do not substitute a literal recipient name or an unsupported placeholder. Company facts, internal notes, titles and standalone FAQ knowledge do not require this token.

Before publication or enrollment, inspect both populated-name and missing-name previews on the actual destination. Final rendered customer text must not contain unresolved tokens or broken punctuation such as `Hi ,`. Use only an existing verified missing-name fallback. If the surface cannot produce a valid preview, report the binding gap and block that item; do not invent a name, conditional syntax or an unimplemented fallback.

A formatter must preserve literal URLs, email addresses, identifiers, numbers, Unicode text and meaningful punctuation. Do not delete every asterisk, underscore or brace. Preserve required Playbook headings/message tags and machine-readable payload structure. Approved API templates must retain their approved text and supported provider mappings; never silently rewrite them as Hosted free text. Plain-text defaults do not authorize rewriting historical conversations or a human's exact approved copy.

## Hard blocks

Do not publish content that contains:
- unresolved SHVYA authoring variables or unsupported runtime placeholders;
- secrets, credentials, private prompts, internal scoring or hidden implementation notes;
- factual prices, policies, offers, deadlines, proof or links without an approved source;
- a promise to send a file, book, call, refund, update CRM or perform another action with no available backend/provider capability and material;
- instructions that conflict with opt-out, human handoff, pipeline routing, qualification or provider restrictions;
- stale example/Ria/other-company facts;
- a recipient name guessed or hard-coded instead of a supported runtime mapping.

## Quality checks

- Answer the customer's substantive question before resuming qualification.
- Keep one clear purpose/CTA per outbound follow-up unless the provider/template requires otherwise.
- Prefer concrete useful material over generic “just checking in” copy.
- Match the organization's approved tone/languages without inventing bilingual behavior.
- Make timing and availability claims only from canonical Calendar/business-hours state.
- Keep operating instructions out of factual knowledge articles.
- Verify file/link availability and sharing eligibility before authoring copy that promises it.
- Check provider template/media/button requirements separately from wording quality.

## Verification

Render/validate the actual destination format, check missing-name behavior, inspect the saved/read-back content, and run the relevant deterministic policy/sequence test. Provider delivery is a separate evidence level.

For a formatting change, record PASS, FAIL or UNKNOWN separately for:
- authored copy and populated/missing-name rendered previews;
- plain-text paragraphs, reply options and preservation of meaningful literal characters;
- verified links, configured languages and Unicode names;
- preserved Playbook structure and approved-template text/mappings;
- relevant AI replies, welcome messages, Cadences, Touchpoints and media captions;
- API, Coexistence, Hosted, Instagram and Sandbox surfaces actually exercised;
- preview text versus the exact final text submitted to the provider.

Do not count an untested surface as passed. A prompt/source contract test proves instruction consistency, not that a model obeyed it, that a runtime formatter enforced it, or that a recipient received it. Keep live-model tests, runtime validation, provider acceptance and recipient delivery as separate evidence. A formatting-only task does not authorize sends, enrollment, activation, timing changes or routing changes.
