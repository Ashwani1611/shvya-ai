# Organization AI Brain JSON bundle

The AI Brain bundle is a generated, versioned JSON snapshot of one organization's
saved AI configuration. The database remains authoritative. Edit the existing
AI Brain, FAQ, pipeline, stage and attribute screens, save those changes, then
download a fresh bundle. The bundle does not introduce another LLM service or an
independently editable configuration store.

## Download

- Dashboard: `GET /dashboard/knowledge-base/ai-brain/download/`
- Authenticated API: `GET /api/v1/ai-engagement/brain-bundle/`

The authenticated organization determines the scope. A supplied organization ID
cannot select another tenant. Responses are private JSON attachments and must
not be cached by shared intermediaries. This feature provides export, not mass
import or automatic configuration writes.

## Schema version 1

`get_organization_ai_brain_bundle(organization=...)` in
`apps/ai_engagement/services/organization_brain_bundle.py` returns:

| Key | Contents |
| --- | --- |
| `kind` | `shvya.organization_ai_brain` |
| `schema_version` | `1` |
| `revision` | Full SHA-256 hash of the canonical exported payload |
| `generated_at` | Generation time, outside the content hash |
| `organization` | Organization ID, name and time zone |
| `ai` | Raw About, Bot Languages, AI Playbook, model identifiers, AI/bump-up controls and record timestamps |
| `qualification` | Raw parsed qualification questions and criteria; the complete authored policy remains in `ai.ai_playbook` |
| `faqs` | All organization FAQ questions, answers, active flags and timestamps |
| `crm.attributes` | Attribute definitions, typed string options, descriptions, order and active flags |
| `crm.pipelines` | Pipeline metadata and nested stages, including descriptions, AI controls and active flags |
| `knowledge.sources` | URL/file source inventory, sanitized source URLs, active flags and timestamps |
| `knowledge.documents` | All document versions, status, sharing readiness, authored sharing instruction and opaque file references |
| `scope` | Explicit data scope, supported configuration and exclusions |

IDs are strings. Text and record collections are not truncated to runtime prompt
budgets. Active and inactive records are included, along with pending and failed
document metadata. A file reference contains the document ID, filename basename
and extension; it contains no raw storage key or temporary download credential.
Source URLs contain host, port and path, excluding user information, query strings
and fragments.

Stage configuration exports only the supported `required_attribute_ids` field.
Those IDs must refer to active, non-sensitive attributes in the same organization.
Arbitrary stage JSON and organization settings are not part of this schema.

## Freshness and runtime use

The generator reads eight tenant-scoped configuration queries: organization,
OrgInfo, FAQs, attributes, pipelines, stages, sources and documents. It never
loads customer records, knowledge chunks, vectors or file bytes.

The revision hashes the exact exported content before any prompt projection.
Changing long authored text, FAQs, models, document versions, active flags or
sharing readiness changes the revision. Generating the same saved content again
changes `generated_at` without changing `revision`. Do not replace this with only
row counts or maximum timestamps: existing publication and readiness writers can
update records without advancing their timestamps.

Runtime organization context uses a bounded projection of the same generated
configuration. The manifest is not an instruction to put every FAQ, source or
document into every model call. Relevant FAQs, knowledge retrieval, guided file
candidates, backend qualification state and action validation keep their existing
runtime limits and authority. Per-turn lead state and channel history remain
separate from organization configuration.

`OrganizationAIRuntimeProfile` version `phase4.v2` records the full
`brain_bundle_revision` and schema version separately from its runtime revision.
The runtime revision also covers existing permission/settings and channel-control
metadata that is intentionally outside the downloadable schema. A new live
context refreshes the profile even when a Lead object is reused. Each Sandbox
turn creates one fresh snapshot and retains it across draft/final composition;
its response exposes only the bundle schema version and revision as diagnostics.
Runtime profile construction uses nine fixed configuration queries, including
the existing WhatsApp account metadata query, with no per-record queries or
knowledge chunk scans. Prompt and candidate limits remain unchanged.

The revision covers configuration and source/document version metadata. It does
not fingerprint binary bytes, extracted chunk text or embedding vectors; those
remain in the canonical storage and knowledge index with their existing version,
publication and repair mechanisms.

## Excluded data

The bundle excludes provider/API credentials, OAuth/session material, arbitrary
settings, raw storage keys, document binaries, chunk contents, embedding vectors,
customer contacts, conversations, lead attributes, memory and execution receipts.
It preserves authored policy as saved; it does not translate, rewrite or train a
separate model on that policy.
