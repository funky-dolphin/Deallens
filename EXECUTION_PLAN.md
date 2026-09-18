# DealLens — Execution Plan

## Objective
Build a Streamlit web application that ingests M&A transaction PDFs, extracts structured fields via Claude API, stores results in SQLite, and surfaces hedging analytics and Q&A — publicly accessible via Streamlit Cloud URL.

---

## Workstreams

### WS1 — Document Ingestion ✅
- File uploader in Streamlit (PDF only)
- Optional source URL input
- SHA-256 checksum for deduplication
- Per-session in-memory SQLite database

### WS2 — LLM Extraction ✅
- PDF sent as base64 to Claude API (no parsing library)
- Structured JSON extraction of 25+ fields across 5 categories
- Fields: transaction identity, timing, conditions, termination, financing
- Confidence scores and evidence quotes per field
- Model: claude-opus-4-5

### WS3 — Field Comparison (Partial)
- Compare 8-K filing summary vs full merger agreement
- Flag conflicts, matches, summary-only fields
- Schema in place (`field_comparisons` table)
- **TODO**: Build UI view and comparison logic

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

---

## Key Decisions

- **SQLite in-memory over ChromaDB/Pinecone**: Simpler, no persistence issues on Streamlit Cloud, sufficient for structured field Q&A
- **Direct PDF base64 to Claude API**: No pdfplumber/PyMuPDF dependency, Claude handles PDF parsing internally
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
