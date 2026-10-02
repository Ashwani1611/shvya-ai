# Knowledge manager domain checks

## Evidence
Inspect source/document identity, version, processing state, active/publication state, chunk/index coverage, FAQ state and any production trace showing retrieval/grounding.

## Known traps
- Upload success is not ingestion success.
- Ingestion success is not publication.
- Publication is not proof the source was retrieved for a turn.
- A filename, URL title or source name does not prove contents.
- Failed OCR/storage/provider quota can look like “AI ignored the file”.
- Re-uploading to fix an uncertain failure can create duplicate/stale versions.
- Knowledge articles must not be used to smuggle operating instructions around Playbook policy.

## Verification
For documents: upload → processing success → publish → health/read-back → response-policy/trace check. For FAQs: read-back exact Q/A and test representative matching. Preserve version history and archive obsolete sources.

## Handoffs
Answer policy → AI Brain/Playbook; ingestion/runtime infrastructure → AI debugger/incident repair; business-fact conflict → Vault/account setup.
