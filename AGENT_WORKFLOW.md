# DealLens — Agent Workflow & Design Decisions

## Agent Used
Claude Sonnet (claude.ai/code) — used for architecture design, code scaffolding, financial concept explanation, and iterative development.

---

## Workflow Log

### Session 1 — Architecture Design
**Decision**: Use in-memory SQLite over ChromaDB for document storage
- **Considered**: ChromaDB + Pinecone for vector similarity Q&A
- **Rejected because**: Streamlit Cloud ephemeral filesystem would wipe ChromaDB on restart; Pinecone adds cost and API key complexity
- **Chosen**: SQLite `:memory:` stored in `st.session_state` — per-session isolation, zero persistence issues, sufficient for structured field lookup

**Decision**: Send PDF directly to Claude API as base64 document
- **Considered**: pdfplumber, PyMuPDF, PyPDF2 to extract text first
- **Rejected because**: Adds dependencies, loses layout/formatting context, lower accuracy on complex legal documents
- **Chosen**: `{"type": "document", "source": {"type": "base64", ...}}` — Claude handles PDF parsing internally

**Decision**: Q&A via SQLite context injection, not RAG
- **Considered**: Embedding chunks into ChromaDB, similarity search on questions
- **Rejected because**: Over-engineered for 25 structured fields; extracted data is already compact and grounded
- **Chosen**: Concatenate all extracted fields as context string, pass to Claude with strict grounding instruction

---

### Session 2 — Scaffolding
Files created:
- `database.py` — SQLite schema and CRUD operations
- `extractor.py` — Claude API PDF extraction pipeline
- `hedging.py` — DV01 calculations and scenario runner
- `app.py` — Streamlit UI with 4 pages

### Session 3 — WS1/WS2 rebuild
The four scaffolding modules above were replaced by the `deallens` package;
`database.py`, `extractor.py` and `hedging.py` no longer exist as top-level
modules. See **Repository layout** in `EXECUTION_PLAN.md` for what replaced
them. `app.py` was rewired onto the package and gained the pricing and
review pages the new controls made possible.

---

## Extraction Prompt Design

The extraction prompt was designed to:
1. Hold the model to an **output schema** keyed by field name, so an omitted
   field is detected per field rather than corrupting the whole response
2. Require **evidence quotes** for every field — these are verified against the
   page they cite, so a quote that is not there withholds the value
3. Require **page numbers** — critical for analyst verification
4. Require **confidence scores** — checked against a threshold that is higher
   for critical fields
5. Cover the 50 registry fields across 6 categories, narrowed to those that
   apply to the detected transaction structure
6. Instruct returning not-found rather than a guess when a field isn't present

The prompt is versioned (`PROMPT_VERSION`) and stamped on every extracted row,
so a result can be tied to the logic that produced it.

---

## Hedging Model Design

The hedging module implements the exact scenario matrix from the assignment:

| Scenario | Strategies |
|----------|-----------|
| Rates +25bps | Unhedged, Forward-Starting IRS, Deal-Contingent |
| Rates -25bps | Unhedged, Forward-Starting IRS, Deal-Contingent |
| Rates +50bps | Unhedged, Forward-Starting IRS, Deal-Contingent |
| Rates +25bps + Credit Spread +20bps | Unhedged, Forward-Starting IRS, Deal-Contingent |
| Base Case Close (85%) | Unhedged, Forward-Starting IRS, Deal-Contingent |
| Delayed Close (10%) | Unhedged, Forward-Starting IRS, Deal-Contingent |
| Transaction Failure (5%) | Unhedged, Forward-Starting IRS, Deal-Contingent |

DV01 formula: `(notional / $100M) × benchmark_DV01_per_$100M`
= ($4B / $100M) × $65,000 = $2,600,000 per basis point

P&L formula: `-DV01 × rate_shift_bps`
(negative because rising rates hurt issuer's position)

---

## Known Gaps / TODO

- [ ] WS3: Filing summary vs agreement comparison view not yet built
- [ ] WS4: Transaction timeline visualization not yet built
- [ ] WS7: Hedging assumptions not yet adapted for Organon and Uber deals
- [ ] Live extraction against the Bio-Techne 8-K not yet run. Ingestion runs
      end to end on it (99 pages, 3 layers, `merger` at 1.00 confidence) and
      the run prices at $1.70-$2.95; the paid call is the step still outstanding
- [ ] Streamlit Cloud deployment not yet done

---

## Error Recovery Patterns

**Malformed or omitted field**: the model is held to an output schema, so a
missing field is detected per field rather than per response. `extractor.py`
records it as `unresolved` and routes it to review; the rest of the layer is
kept. A layer that fails outright is recorded as a warning on the run and the
remaining layers still extract.

**API failure**: Caught by generic `except Exception` in extractor. Error bubbled to UI.

**Withheld value**: a field that was found but failed normalization, evidence
verification or the confidence threshold keeps its raw value and evidence so a
human can adjudicate, but its normalized value is not asserted. It is routed to
the review queue. Q&A only ever sees fields whose status is `found`, so a
withheld value cannot be laundered into an answer.
