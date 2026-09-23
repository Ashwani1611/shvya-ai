# Shvya variables

`variable-registry.json` defines package-owned authoring values. `shvya-example.values.json` supplies a complete Ria example grounded in the user's attached reference. Uppercase `{{SHVYA_*}}` tokens are replaced when building artifacts; they are not native CRM variables or automatically created Shvya records. The package does not store secrets, call permissions, or cross-tenant identifiers in prompts.

Render only the selected template's tokens, one literal substitution pass. Strings are inserted unchanged; arrays/objects/booleans/numbers are JSON serialized when a template needs them. Reject unknown names, referenced null/empty required values, type mismatches, and remaining `{{SHVYA_*}}` tokens. Do not recursively render token-looking text supplied as a value. The rendered Playbook must remain under Shvya's 100,000-character limit and contain exactly its eight canonical top-level sections.

Keep IDs null in reusable examples until a real organization-owned record is discovered. A null example value is not suitable for a live MCP call. Authorization comes from the user and the server's effective capability policy, never from an `approved` variable. `SHVYA_DRY_RUN=true` and `SHVYA_ENABLE_AUTOMATIONS=false` are example planning values; no render defaults are applied; changing them is not itself approval to mutate or send.

## Native runtime personalization is separate

The inspected Shvya renderer exposes `{{lead_name}}`, `{{lead_first_name}}`, `{{phone}}`, `{{email}}`, `{{lead_source}}`, `{{org_name}}`, `{{user_name}}`, `{{pipeline_name}}`, and `{{stage_name}}`, plus exact organization attribute keys returned by the tenant-safe placeholder catalog. The nine listed built-in lowercase runtime tokens survive setup rendering. Custom attribute tokens are rejected by this pure authoring service because it cannot prove their tenant ownership; handle them only through a discovered tenant-aware native delivery surface. Prefer double braces consistently. Legacy single-brace rendering is supported in Hosted paths but is not the package's authoring convention.

Source verification: `services/followup_service.py::_lead_template_values` and `services/channels/template_service.py::available_placeholders` / `render_template_body` in the inspected Shvya checkout. This proves the listed code baseline; rediscover the active environment before changing a live account. Display names of attributes are not automatically valid placeholder keys. Meta/API WhatsApp templates use approved positional parameter mappings to these values; a free-form message is not converted into an approved API template by inserting braces.

Use personalization only when the delivery surface supports it. Check empty-name behavior and omit name-based greetings when data is missing. Keep CRM stage/pipeline names, phone/email, user identity, and internal custom attributes out of customer copy unless there is a specific approved customer-facing reason. The Ria Playbook's static customer copy needs no runtime tokens.

## Changing organizations

Replace the brand/persona, languages, time zone, approved business description, messages, qualification criteria, mappings, stage logic, reminder logic, and authorized contacts together. The example's Ria-specific four questions and contacts are not defaults for unrelated customer companies. Full section-block variables make the generic template reusable for different business models without renaming parser headings.

`SHVYA_QUESTION_BLOCKS` holds complete `<question_content>` blocks, with one actual question and its options per block. Put private mapping/branching/notes outside customer tags and in the appropriate section. `SHVYA_QUALIFICATION_CRITERIA` uses explicit supported predicates; never replace missing requirements with majority-answered or AI-score shortcuts. All action-bearing blocks still require backend validation after rendering.

## Native authoring tools

Use `get_setup_variable_schema` for the complete typed 41-variable contract. Call `render_setup_template` with `template_id` (`ai-playbook`, `company-about`, `voice-agent`, `voice-call-instructions`) and an explicit `variables` object. Unknown names, wrong types, referenced nulls, missing values, unsupported placeholders and malformed section/tag structure fail before output. Even `SHVYA_EXTRA_RULES` must be explicitly supplied when used; an empty string is valid. AI Playbook drafts are checked by the canonical Shvya Playbook and qualification compiler. This proves syntax, not factual correctness, tenant IDs, runtime behavior or permission to save. Voice drafts do not provision an agent.
