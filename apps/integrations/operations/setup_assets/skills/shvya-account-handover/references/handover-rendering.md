# Handover rendering contract

## Scope

Use `render_account_handover` only when discovered in the current authorized MCP catalog. It converts a strict document model to matching Markdown and self-contained printable HTML. It does not collect configuration, independently verify facts, run tests, persist files, publish URLs or send messages. The active organization is server-derived; the supplied organization_id must match it. The title uses the actual organization name.

Capability: `setup.artifacts.prepare`; read OAuth scope. Existing policies and explicit Superadmin context still apply. Rendering does not expand authority.

## Tool arguments and complete artifact assembly

- `document`: version 1 object described below, identical across requests.
- `format`: `html` or `markdown`.
- `offset`: zero-based character offset, initially 0.
- `limit`: 1–20000 characters, default 20000.

Read `text`, then call again at `next_offset` until it is null. Reassemble in order, with no inserted separators or text transformations. Compare `document_sha256` and `artifact_sha256` on every page; verify `chunk_sha256` on UTF-8 bytes and the assembled artifact hash before saving. Offsets count Python Unicode characters, not UTF-8 bytes; always use returned offsets rather than calculating them from a JavaScript UTF-16 length. A response with `truncated: true` is a chunk, not a complete deliverable.

Render both formats from the identical document. The document hash includes the organization name. If configuration/identity or the model changes between calls, restart assembly. The tool returns `suggested_filename`, not a saved file or download URL. Save reviewed files through an available artifact/file tool. If this fails, state what remains unsaved.

The response also identifies MIME type, total/returned characters and validation facts. `evidence_independently_verified` is always false, `script_only` true and `persisted` false. Reported readiness is caller-supplied evidence, not a server certification or test run.

## Top-level document

All keys are required; extra keys are rejected:

| Field | Contract |
|---|---|
| schema_version | Integer 1. |
| organization_id | Exact selected organization UUID as a string. |
| environment | Sourced environment label; use unverified when not established. |
| evidence_date | ISO datetime with explicit timezone offset for evidence capture. Not a claim of an atomic snapshot. |
| timezone | IANA timezone from the account. |
| readiness | configuration, engine_test, live_delivery, evidence, note. |
| sources | Nonempty source ledger, up to 200 records. |
| sections | Exactly overview, flow, qualification, automation, demo, day_to_day, open_questions, not_covered. Each is a nonempty block array. |

Readiness configuration is `configured`, `partial`, `not_configured` or `unknown`. Engine/live states are `passed`, `failed`, `not_tested` or `unknown`. The `evidence` list cites source IDs; `note` explains coverage. A claimed pass requires the collector to supply the existing test evidence; the renderer only checks structure.

Each source contains exactly:
`id, tool, reference, observed_at, status, note`.

- ID: unique short alphanumeric/underscore/hyphen identifier (maximum 64 characters).
- Tool/reference: exact returned object IDs, tool and revision/reference, or a client/Vault source. No signed private URLs or credential values.
- observed_at: ISO datetime with explicit offset.
- status: `complete`, `partial`, `unavailable` or `conflict`.
- note: pagination coverage, returned sanitation limits, requirement-versus-state distinction and uncertainty.

Every block, table row, transition and demo/Cadence step has a nonempty `evidence` list. Every referenced ID must exist in sources. Missing inspection is represented by an unavailable source plus an explicit unknown paragraph, not a fabricated zero or a false completeness claim.

## Block shapes

All fields listed are required. All supplied text is literal, not arbitrary Markdown/HTML/template code. Do not insert HTML or Mermaid strings; supply structured stages and transitions.

### paragraph

`{type:"paragraph", text, evidence:[source IDs]}`

### list

`{type:"list", title, items:[text], evidence:[source IDs]}`

### table

`{type:"table", title, columns:[text], rows:[{cells:[text], evidence:[source IDs]}], evidence:[source IDs]}`

Each row's cell count must equal the column count. Up to 12 columns and 200 rows. Split larger inventories into additional blocks without dropping records. Use exact IDs plus friendly names in appropriate cells. The renderer appends an Evidence column.

### journey

`{type:"journey", title, pipeline, stages:[{id,label,state}], transitions:[{from,to,kind,condition,evidence}], evidence}`

- state: `ai`, `human`, `won`, `lost`, `parked` or `unknown`.
- kind: `automatic`, `human` or `requested`.
- from/to: stage IDs within this one journey; unique stage IDs, no dangling edges.
- condition: exact meaningful rule condition, not an inferred stage-order move.

Markdown has generated top-down Mermaid plus a transition table. Standalone HTML uses colored stage cards, directed transition lanes and the same table; it needs no remote Mermaid script. Automatic is solid, human dashed, requested dotted/not configured. Color is not the only meaning. Use the surrounding explanation to distinguish parked-stage AI permission from effective channel readiness.

### demo

`{type:"demo", title, prerequisites, fixture, steps:[{kind,action,expect,dashboard,why,say,evidence}], evidence}`

kind is `send` or `do`. A timer walkthrough uses `do` and an action beginning "Explain only: ..."; do not ask the operator to wait, shorten production delays or send a live message. Every step is labeled script-only.

Use real account-specific inputs only after source inspection. Expected responses are paraphrases. Dashboard identifies saved observable outcomes; Why explains their cause; Say is the presenter line. Fixture is an exact authorized disposable plan or an explicit target-not-approved note, never a random customer.

### gap

`{type:"gap", title, severity, observation, impact, decision, evidence}`

severity is `blocker`, `warning` or `question`. Include uncertainty, named owner if known and a real/not-agreed deadline in decision. Distinguish verified defects from missing evidence.

### cadence

`{type:"cadence", title, channel, start, stop, end, schedule, pause, delay_basis:"after_previous", steps:[{delay:{value,unit},message,evidence}], evidence}`

value is a bounded nonnegative integer; unit is `minutes`, `hours` or `days`. Normalize only when the saved timing semantics are known. The renderer computes cumulative offsets exactly; for 30 minutes, 1 day and 2 days it reports 3 days 30 minutes. Schedule windows and reply pauses can defer delivery, so these offsets are not receipt promises.

Use message for a faithful message/template/attachment summary with relevant step ID and bindings. Preserve channel/sender and replace/completion behavior in the other fields. Up to 100 steps per block. For more than 100 steps, unknown anchors or unsupported units, use sourced generic tables that explicitly show the full sequence and its uncertainty; do not silently reset cumulative timing or invent normalized values.

## Bounds and security

At most 250000 serialized input characters, 300 total blocks, 100 blocks per section, 25000 JSON nodes, structural depth 12 and 1000000 output characters. Text fields have additional bounds (generally 8000, table/demo/Cadence text 4000, titles 240). Oversize/invalid input fails; it is not silently truncated into an apparently complete artifact.

Credential-like text is rejected before rendering, and all rendered customer strings are escaped. Data/executable/file URLs and URLs with credentials, queries or fragments are unsupported. Supply stable public references or object IDs; never make a private asset public just to satisfy the renderer. There are no external scripts/fonts/assets or arbitrary template evaluation.

## Structural example only

The following deliberately contains unknowns. Replace its fictional identity, time and unavailable evidence with the selected account's real sources. It is not acceptable as a completed client handover.

```json
{
  "schema_version": 1,
  "organization_id": "00000000-0000-4000-8000-000000000001",
  "environment": "unverified",
  "evidence_date": "2026-10-11T12:00:00+05:30",
  "timezone": "Asia/Kolkata",
  "readiness": {
    "configuration": "unknown",
    "engine_test": "not_tested",
    "live_delivery": "not_tested",
    "evidence": [
      "pending"
    ],
    "note": "Illustration only. No account inspection or test has been performed."
  },
  "sources": [
    {
      "id": "pending",
      "tool": "not inspected",
      "reference": "No selected organization evidence",
      "observed_at": "2026-10-11T12:00:00+05:30",
      "status": "unavailable",
      "note": "Replace with actual source records before delivering a handover."
    }
  ],
  "sections": {
    "overview": [
      {
        "type": "paragraph",
        "text": "Not inspected. This is a structural example, not a completed handover.",
        "evidence": [
          "pending"
        ]
      }
    ],
    "flow": [
      {
        "type": "paragraph",
        "text": "Not inspected. This is a structural example, not a completed handover.",
        "evidence": [
          "pending"
        ]
      }
    ],
    "qualification": [
      {
        "type": "paragraph",
        "text": "Not inspected. This is a structural example, not a completed handover.",
        "evidence": [
          "pending"
        ]
      }
    ],
    "automation": [
      {
        "type": "paragraph",
        "text": "Not inspected. This is a structural example, not a completed handover.",
        "evidence": [
          "pending"
        ]
      }
    ],
    "demo": [
      {
        "type": "paragraph",
        "text": "Not inspected. This is a structural example, not a completed handover.",
        "evidence": [
          "pending"
        ]
      }
    ],
    "day_to_day": [
      {
        "type": "paragraph",
        "text": "Not inspected. This is a structural example, not a completed handover.",
        "evidence": [
          "pending"
        ]
      }
    ],
    "open_questions": [
      {
        "type": "paragraph",
        "text": "Not inspected. This is a structural example, not a completed handover.",
        "evidence": [
          "pending"
        ]
      }
    ],
    "not_covered": [
      {
        "type": "paragraph",
        "text": "Not inspected. This is a structural example, not a completed handover.",
        "evidence": [
          "pending"
        ]
      }
    ]
  }
}
```

Follow [the handover template](handover-template.md) and [demo authoring guide](demo-script.md) for the required depth of content. Rendering success is not visual QA: preview both formats and check long tables, navigation, script readability and print output when tooling is available.
