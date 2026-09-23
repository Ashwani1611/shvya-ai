# Authoring pass kickoffs

These are reusable orchestration messages for a host-loaded skill. The current Shvya server is tools-only; these files do not install MCP `prompts/list` or `prompts/get` by themselves. Use the optional kit integration separately if implemented and deployed.

## Profile

Build the Client Profile from the provided company materials and source inventory. Extract actual products, customer segments, qualification definition, numeric facts with units, approved claims, objections, language, handoff owners and assets. Preserve each commitment verbatim with its date/deadline/owner. Do not re-ask supplied facts. Return conflicts and gaps separately; do not promote legacy examples into company facts.

## Qualification and Playbook

Use the profile, preferences, existing Shvya inventory and qualification builder prompt. Produce the canonical eight sections of AI Playbook, About, bot_languages, structured requirements and mappings, FAQs, knowledge manifest and builder notes. Use stable Shvya requirement IDs. A conditional branch references a prior requirement and `eq` only. Respect backend evidence and execution rules. Return proposed artifacts only.

## Cadence outline

Repeat a compact business audit: company, B2B/B2C/both, actual offerings, buyer, sales cycle, drop-offs, language/script, channels, opt-out policy and concrete CTA outcomes. Design the smallest adequate set of Cadences. For every message give purpose, new asset/fact with source, CTA, relative delay and cumulative offset, sender/provider, entry and stop conditions. Mark missing assets instead of inventing proof or urgency. Do not write full copy yet.

## Cadence writer

Write every message from the outline in the company's customer language and register. Use short paragraphs separated by blank lines, WhatsApp `*bold*` where useful and one option per line. Each message offers one sourced fact or asset and one clear next step. Keep internal purpose labels in metadata, outside the message body. Resolve kit variables before publication; retain only verified native Shvya placeholders. Use a safe greeting if the recipient name might be missing. Opt-out text must correspond to a tested suppression path. Return source notes outside customer copy.

## Account builder

Use the profile, final Playbook, structured qualification, Cadence outline/copy and inventory to produce the Shvya configuration plan. Include stage meanings/AI ownership, exact attribute mappings, event-condition-action Workflows, sender-bound Cadences, Touchpoint categories, settings, dependencies, capability gates and verification cases. Discover live schema before making payloads. Produce no invented IDs, APIs, supported actions or success claims. Package mode stops at local artifacts.
