# DealLens — Execution Plan

## Objective
Build a Streamlit web application that ingests M&A transaction PDFs, extracts structured fields via Claude API, stores results in SQLite, and surfaces hedging analytics and Q&A — publicly accessible via Streamlit Cloud URL.

---

## Workstreams

### WS1 — Document Ingestion ✅
- File uploader in Streamlit (PDF only), optional source URL
- SHA-256 checksum for deduplication
- Page inventory with per-page text, character counts and image detection
- Layer segmentation: filing summary vs agreement exhibit vs governing documents
- Transaction-structure classification (merger / tender offer / scheme …)
- Page-integrity controls: duplicate, blank, sparse and unreadable pages;
  printed-label reconciliation. Unreadable pages block extraction outright
- Per-session in-memory SQLite database

### WS2 — LLM Extraction ✅
- Machine-readable filings extracted as text; only image-only pages fall back
  to page images, which is what cut input cost 54%
- Each layer queried separately so the summary and the agreement can be
  compared in WS3 rather than blended
- 50 fields across 6 categories, narrowed per transaction structure
- Requests sized against the token budget; the schema is re-sent per request,
  so chunking is avoided rather than merely tolerated
- Fail-closed controls: a value is asserted only when it normalizes cleanly,
  its evidence quote is found on the page it cites, and confidence clears the
  threshold (higher for critical fields). Everything else routes to review
- Spend is estimated and gated before the first request
- Model: claude-opus-5

### WS3 — Field Comparison ✅
- Each field's filing-summary and agreement readings are compared and
  classified into the assignment's seven classes: match, normalized match,
  summary only, agreement only, conflict, not applicable, unresolved
- `match` and `normalized match` are told apart on the source text, not the
  normalized value: "$250 million" and "$250,000,000" both normalize to the
  same float, and reporting them as a plain match would hide that the two
  documents state the term differently
- Both values and both source locations are preserved on every comparison,
  including conflicts
- Source hierarchy: the operative agreement governs, because it is the
  executed contract and the filing summary is a description of it. The
  nomination is recorded; the other reading is never discarded
- Computed on demand rather than stored — the review queue can change a
  field's status, and a persisted comparison would be stale immediately.
  There is no `field_comparisons` table, and an earlier claim here that the
  schema was in place was wrong
- Surfaced on the **Summary vs. agreement** page, with a JSON export

### WS4 — Transaction Timeline
- Outside date, expected closing, extension conditions
- Risk event map
- **TODO**: Build timeline visualization in Streamlit

### WS5 — Hedging Analytics ✅
- DV01 calculation for $4B notional, 7-year tenor
- Rate shift scenarios: ±25bps, +50bps
- Parallel rate + credit spread scenario
- Transaction outcome scenarios: base close, delayed, failure
- Three strategies: Unhedged, Forward-Starting IRS, Deal-Contingent
- All inputs labeled as synthetic assumptions

### WS6 — Document Q&A ✅
- 12 preset questions from assignment
- Custom question input
- Claude answers grounded in extracted SQLite fields
- Evidence citations and page references required

### WS7 — Generalization
- Bio-Techne (primary)
- Organon / Dermavant (validation 1)
- Uber / Delivery Hero (validation 2)
- Adapted hedging assumptions per deal

### WS8 — Controls & Auditability
- Run ID on every extraction
- Confidence scores per field
- Evidence quotes from source document
- All synthetic assumptions clearly labeled

---

## Time Budget (Friday afternoon → Monday morning)

| Phase | Est. Time |
|-------|-----------|
| Scaffolding (done) | 2h |
| End-to-end test with Bio-Techne PDF | 2h |
| WS3 comparison view | 1h |
| WS4 timeline view | 1h |
| Deploy to Streamlit Cloud | 1h |
| Test with Organon + Uber PDFs | 2h |
| Technical memo (3 pages) | 2h |
| Polish + Git history cleanup | 1h |

---

## Deployment

1. Push to GitHub (public or private)
2. Connect repo to Streamlit Cloud at share.streamlit.io
3. Set `ANTHROPIC_API_KEY` in Streamlit Cloud secrets
4. App accessible at `https://<your-app>.streamlit.app`

Note: On Streamlit Cloud the in-memory SQLite resets on each browser session — this is by design. Each user gets a clean isolated session.

For local development, set `DEALLENS_DB` to a path (e.g. `DEALLENS_DB=deallens.db`) to keep that database on disk. A refresh starts a new Streamlit session and discards an in-memory database along with the extraction it holds, and re-running an extraction is the only step that spends money. With a file-backed database everything downstream of extraction stays browsable across refreshes; a new extraction still needs a re-ingest first, which is local and free. Leave it unset on a shared deployment, where one file would be one database shared by every visitor. The sidebar reports which mode is active.

---

## Repository layout

```
app.py                      Streamlit UI: ingest → price → extract → review
deallens/
  ingestion/                WS1
    loader.py               PDF → page inventory, checksum, text layer
    classifier.py           layer segmentation + structure classification
    integrity.py            page-integrity controls, printed-label reconciliation
    locators.py             source locators, evidence verification
    pipeline.py             ingest(): the one entry point
  extraction/               WS2
    registry.py             the 50 field specs and what they apply to
    prompts.py              system prompt + per-layer user prompt + output schema
    client.py               Claude call, token budget, pricing constants
    normalize.py            money/date/percent normalization
    models.py               ExtractedField + the fail-closed rules
    extractor.py            estimate_run(), extract_layer(), extract_document()
  comparison.py             WS3 summary vs. agreement classification
  analytics/hedging.py      WS5 DV01 and scenario matrix
  db/                       schema + repository (the audit record)
scripts/estimate_cost.py    price a run from the CLI, offline
tests/                      118 tests
```

## Key Decisions

- **SQLite in-memory over ChromaDB/Pinecone**: Simpler, no persistence issues on Streamlit Cloud, sufficient for structured field Q&A
- **Text over page images where the filing allows it**: the original design sent the whole PDF as base64. Sending extracted text for machine-readable filings, and reserving page images for pages that genuinely need them, cut extraction input from 290,717 to 133,128 tokens for the same document (127,356 since prompt 3.0.0 moved the field descriptions out of the schema)
- **Per-session DB in st.session_state**: Isolates users, no cross-contamination
- **Synthetic assumptions clearly labeled**: All hedging inputs flagged as synthetic unless extracted from document

---

## Risks

| Risk | Mitigation |
|------|-----------|
| Claude returns malformed JSON | Strip markdown fences, fallback error message |
| PDF too large for API | Warn user, suggest smaller file |
| Streamlit session reset loses data | Expected behavior, document in UI |
| API rate limits | Single user app, unlikely to hit |
