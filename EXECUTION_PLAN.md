# DealLens — Execution Plan

## Objective
Build a Streamlit web application that ingests M&A transaction PDFs, extracts structured fields via Claude API, stores results in SQLite, and surfaces hedging analytics and Q&A — publicly accessible via Streamlit Cloud URL.

---

## Workstreams

### WS1 — Document Ingestion — complete
- File uploader in Streamlit (PDF only), optional source URL
- SHA-256 checksum for deduplication
- Page inventory with per-page text, character counts and image detection
- Layer segmentation: filing summary vs agreement exhibit vs governing documents
- Transaction-structure classification (merger / tender offer / scheme …)
- Page-integrity controls: duplicate, blank, sparse and unreadable pages;
  printed-label reconciliation. An unreadable page blocks extraction of the
  layer it sits in, not the whole document
- Per-session in-memory SQLite database

### WS2 — LLM Extraction — complete
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

#### Field coverage: 36 bullets, 50 fields

The assignment lists **36 bullets** across six categories. The registry holds
**50 fields**, because several bullets bundle values that have to be extracted
separately. Every bullet is covered.

| Category | Assignment bullets | Registry fields | Where a bullet splits |
|---|---|---|---|
| Transaction identity | 7 | 10 | "Consideration type, amount, and currency" is three fields |
| Timing | 5 | 5 | — |
| Conditions | 6 | 9 | "Shareholder approval or tender threshold" is two; "Antitrust, foreign-investment, and other regulatory approvals" is three |
| Termination and fiduciary | 6 | 8 | "Fee triggers and fee tails" is two; "Matching rights and termination rights" is two |
| Financing | 6 | 10 | Each of the first four bullets pairs two values (amount/currency, maturity/interest basis, …) |
| Equity treatment | 6 | 8 | "Vested and unvested options" is two; "RSUs and PSUs" is two |
| **Total** | **36** | **50** | |

Splitting earns its keep in each case. `bridge_amount` and `bridge_currency`
need different normalizers. Vested and unvested options are routinely treated
differently in the same agreement — cashed out against rolled over — so one
field would force the model to blend two answers into a sentence no
downstream consumer could use. And separating the shareholder vote from the
tender threshold is what lets `applies_to` mark the irrelevant one
`not_applicable` rather than leaving it a failed extraction.

One field goes **beyond** the assignment's list: `total_transaction_value`,
the aggregate equity or enterprise value where the document states one. It is
context a reader wants and costs nothing to carry.

How many run against a given document, after `applies_to` narrows the set:

| Structure | Applicable | Excluded |
|---|---|---|
| `merger` | 48 | `offer_or_acceptance_period`, `tender_acceptance_threshold` |
| `tender_offer` | 49 | `shareholder_approval_threshold` |
| `takeover_offer` | 48 | `shareholder_approval_threshold`, `merger_subsidiary` |
| `scheme_of_arrangement` | 48 | `offer_or_acceptance_period`, `tender_acceptance_threshold` |
| `unknown` | 50 | nothing — an uncertain structure excludes nothing |

46 of the 50 apply to every structure; only four are structure-dependent. **12
are marked `critical`** and held to a 0.75 confidence bar rather than 0.60.

### WS3 — Field Comparison — complete
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

### WS4 — Transaction Timeline — complete
- Most of what this workstream covers is not a calendar date. Entries are
  classified into the assignment's six kinds — fixed, relative, conditional,
  automatically extended, party election, non-binding estimate — and only the
  ones that resolve are placed in the ordered timeline. "30 days after written
  notice" has no calendar position until the notice exists, and giving it one
  would invent a date
- A second axis, `basis`, records whether a date was `extracted` or
  `calculated`. They are different questions: a derived extension date is a
  fixed calendar date whose basis is arithmetic
- Extension dates are calculated from the agreement's own clause and carry a
  `derivation` string showing the working (`2027-06-25 + 2 x 90 days`), plus
  the page and quote of the clause they came from. Where the clause cannot be
  parsed with confidence, nothing is calculated and the entry says so
- Produces the **hedge horizon** — signing to outside date, extension days,
  worst-case close, conditions outstanding — which WS5 consumes rather than
  deriving its own close date
- Surfaced on the **Timeline & risk map** page, with a JSON export

### WS5 — Hedging Analytics — complete
- Seven scenarios x three strategies, with every result attributed across the
  seven risks the assignment requires be kept apart: benchmark rate, swap
  spread, issuer credit spread, FX, timing, completion, unwind
- A zero in a risk column means the strategy is not exposed to it in that
  scenario, not that it was ignored — the two are different statements and the
  table has to tell them apart
- Hedges are not free: rates -25bp shows the unhedged issuer +$65mm and the
  hedged issuer flat, because the hedge gives up the upside with the downside.
  Neither hedge covers the issuer's own credit spread, so scenario 4 costs all
  three strategies the same -$52mm
- Timing comes from WS4's horizon. Transaction failure leaves the conventional
  swap to be unwound and the deal-contingent structure to terminate at no
  breakage cost — the reason that structure exists
- Sign convention stated on the page: positive is a gain to the issuer, and
  the coupon is not yet fixed, so a rise in rates is a loss
- Inputs the assignment does not supply are in `ADDITIONAL_ASSUMPTIONS`, each
  with the reason it is required, versioned and shown in the UI
- A separate exposure table carries risks no required scenario shocks — swap
  spread and FX — because a risk a strategy carries is still a risk it carries

### WS6 — Document Q&A — complete
- The twelve assignment questions, plus free-text. A custom question changes
  nothing about what the model can see: the context is the same fixed set of
  asserted fields either way, so there is no retrieval step a question could
  widen
- Answers are built from extracted fields, never the document. By the time a
  value reaches Q&A it has been normalized, its quote checked against the page
  it cites, and its confidence tested. Answering from raw text would route
  around every one of those controls
- A document with no asserted fields returns the assignment's exact
  unsupported-answer sentence without calling the API
- Prompt-injection defence: instructions live in the system prompt, extracted
  data is fenced in the user turn and declared untrusted. Evidence quotes are
  document text, and a filing is a public document anyone can draft
- One API call per question, uncached — see Known Gaps in `AGENT_WORKFLOW.md`

### WS7 — Generalization
- Bio-Techne / Merck KGaA (development)
- Organon / Sun Pharma (validation 1)
- Uber / Delivery Hero (validation 2)
**Ingestion run against all three (extraction still outstanding):**

| | Bio-Techne | Organon | Uber / Delivery Hero |
|---|---|---|---|
| pages | 99 | 109 | 149 |
| structure | `merger` 1.00 | `merger` 0.73 | `takeover_offer` 0.87 |
| machine-readable | yes | yes | no |
| layers | 3 | 4 | 5 |
| may extract | yes | yes | **no** |
| estimated cost | $1.64–2.89 | $1.74–2.99 | $1.25–2.50 |

- **The classifier identified the German takeover offer unprompted**, on
  evidence it found itself: "voluntary public takeover offer",
  "Wertpapiererwerbs" (the German Takeover Act), "BaFin", "acceptance
  period". No transaction-specific code was added for it
- Organon classifies as `merger` at 0.73 against Bio-Techne's 1.00 — correct,
  but on thinner evidence, and worth reporting rather than rounding up
- **Uber blocks on one page in 149.** Page 148 is slide 15 of the investor
  presentation in `exhibit-ex99.2`: a single flattened 1095x614 JPEG with a
  three-whitespace-character text layer. 148 of 149 pages yield text; that one
  needs OCR, which this pipeline does not do
- The block is **correct but coarse**, and is being left in place
  deliberately. `may_extract` asks "is any page in this file unreadable?" when
  the governing question is "is any page in the layers I am about to read
  unreadable?" The offending page sits in a layer extraction never opens, so a
  Business Combination Agreement is being refused over a chart in a slide
  deck. Narrowing the scope is a roadmap item rather than a quick fix: it
  loosens a fail-closed rule, and a validation case that surfaces the
  distinction is worth more as a finding than as a patch
**Schema extension #1: financing agreements (applied).** Uber attaches its
bridge facility as Exhibit 10.1 — confirmed as a `BRIDGE CREDIT AGREEMENT`
dated 16 July 2026, Uber as borrower, Morgan Stanley Senior Funding as
administrative agent, 83 pages. It matched no instrument signature, so it fell
through to a generic `exhibit`, and `EXTRACTABLE_LAYERS` covered only
`filing-summary` and `agreement`. The whole financing category — ten fields —
had no source, and Uber priced *below* Bio-Techne despite being half again as
long, because it was costing 43 of 149 pages.

Nothing had failed. Ingestion segmented the layer, read all 83 pages, stored
their text, and honestly reported it as an unidentified exhibit. The gap was a
whitelist in extraction drawn around the development filing, whose only other
exhibit is a two-page charter. Against that document, "these two exhibit kinds
are not sources" and "nothing else is a source" are indistinguishable. Uber is
the first document where they diverge — which is what an out-of-sample case is
for.

Two modular changes, neither specific to Uber:

- a `credit-agreement` instrument signature matching `BRIDGE CREDIT
  AGREEMENT`, `CREDIT AGREEMENT`, `FACILITIES AGREEMENT`, `COMMITMENT LETTER`
  and `INTERIM FACILITIES AGREEMENT`, ordered after the operative-agreement
  signature so a merger agreement still classifies as `agreement`
- the layer added to `EXTRACTABLE_LAYERS`, scoped through
  `LAYER_FIELD_CATEGORIES` to the financing category only. A credit agreement
  has its own material adverse effect clause, conditions precedent and
  termination provisions, all about the loan; asking it the full field set
  would return confident answers to merger questions from the wrong contract

Uber now reads **126 of 149 pages** (was 43) at $2.34–4.22 (was $1.25–2.50).
Bio-Techne and Organon are unchanged — neither attaches a financing agreement.
`deal_from_rows` also needed to fall back beyond the summary-vs-agreement
comparison, or a `bridge_currency` read from the financing layer would never
have reached the FX exposure it exists to drive.

- **TODO**: extraction against all three, then the cross-transaction analysis.
  That analysis is a report on how the *pipeline* coped, not a comparison of
  deal terms: what generalized, what needed a schema extension, what failed,
  and extraction performance by document
- The architecture anticipates it — `applies_to` already narrows the field set
  per structure, the classifier has tender-offer and German-takeover paths,
  `normalize_currency` handles EUR, and FX exposure reads the extracted
  consideration currency. WS7 proves that rather than building it

### WS8 — Controls & Auditability — partial
- **Input**: checksum, source URL, filing date, ingestion timestamp, version
  and run id on every document; duplicate, blank, sparse and unreadable page
  detection, with unreadable pages blocking extraction outright
- **Extraction**: schema validation, deterministic normalization, evidence
  verified against the cited page, conflict detection across layers,
  confidence thresholds higher for critical fields, and a human review queue
- **Review**: a reviewer can correct a withheld field. The value is normalized
  by the same code, the quote re-verified, `extraction_method` set to `manual`
  or `hybrid`, and the model's superseded reading kept in the notes — the
  assignment requires material manual corrections be disclosed
- **Analytics**: versioned assumptions (`ASSUMPTIONS_VERSION`) and stable
  scenario identifiers; explicit sign convention; 28 unit tests
- **AI**: model and prompt version recorded on every extracted field, whichever
  model ran; prompt-injection defence in both extraction and Q&A
- **Output**: fact / assumption / analysis labels in Q&A answers, an exception
  report in the review queue, JSON exports from the comparison, timeline and
  scenario pages
- **TODO**: no stale-input warning; no consolidated audit export across a whole
  document

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
                            EXTRACTABLE_LAYERS / LAYER_FIELD_CATEGORIES
                            in extractor.py decide which layer answers what
    prompts.py              system prompt + per-layer user prompt + output schema
    client.py               Claude call, token budget, pricing constants
    normalize.py            money/date/percent normalization
    models.py               ExtractedField + the fail-closed rules
    extractor.py            estimate_run(), extract_layer(), extract_document()
  comparison.py             WS3 summary vs. agreement classification
  timeline.py               WS4 date kinds, calculated dates, hedge horizon
  analytics/hedging.py      WS5 scenarios x strategies x risk separation
  qa.py                     WS6 grounded answers over asserted fields
  review.py                 WS8 manual correction of a withheld field
  db/                       schema + repository (the audit record)
scripts/estimate_cost.py    price a run from the CLI, offline
.streamlit/config.toml      theme: navy, red, white
tests/                      216 tests
```

## Data flow

Extraction writes to `extracted_fields`. Every section of the application
reads from `extracted_fields`. Nothing downstream re-reads the PDF, and no
section filters by which layer a row came from.

```
  PDF ──> ingestion ──> documents / document_pages / document_layers
                             │
                             ▼
                        extraction ──> extracted_fields
                                            │
            ┌───────────────┬───────────────┼───────────────┬───────────────┐
            ▼               ▼               ▼               ▼               ▼
      WS3 comparison   WS4 timeline    WS5 hedging      WS6 Q&A       WS8 review
      summary vs.      date kinds,     currency from    context from   exceptions +
      agreement        horizon         the rows         asserted rows  conflicts
                            │               ▲
                            └── horizon ────┘
```

Each reader derives, it does not re-extract. The comparison classifies pairs
of rows; the timeline classifies dates among them and computes the hedge
horizon; hedging consumes that horizon rather than deriving its own close
date; Q&A builds its context from asserted rows only; review surfaces the rows
a control withheld plus the pairs the comparison finds in conflict. A manual
correction writes back to the same table, so it reaches all five.

Only two decisions happen *before* the table, and both are about which
document is asked what:

- **`EXTRACTABLE_LAYERS`** — which layers are opened at all. A press release
  and a charter exhibit are ingested, stored and searchable, but they cannot
  tell you what the contract says.
- **`LAYER_FIELD_CATEGORIES`** — what each opened layer is a source *for*.
  Classifying a layer tells you what kind of document it is, and that decides
  which questions it can answer. The filing summary and the agreement answer
  all of them; a financing agreement is authoritative but on a narrower set.

---

## Database schema

SQLite, created by `deallens/db/schema.py`, version `1.0.0`. **The schema is
the audit record**, not a cache: every table carries the `run_id` that
produced its rows, so a reviewer can reconstruct what a given run saw and
concluded, and re-running with changed logic produces a new run rather than
overwriting the old one.

| Table | Grain | What it is for |
|---|---|---|
| `documents` | one per file | Identity and ingestion verdict: checksum, source URL, filing date, page count, machine-readable, OCR required, ingestion status, classified structure and its confidence |
| `document_pages` | one per PDF page | Both page numbers, character count, text-layer status, content hash, image flag — **and the page text itself** |
| `document_layers` | one per layer | Filing summary, agreement exhibit, press release …, with page boundaries, how it was detected, and the evidence for that |
| `document_regions` | one per region | Sub-areas within a layer |
| `integrity_issues` | one per finding | Duplicate, blank, sparse or unreadable pages, with severity and the pages affected |
| `structure_evidence` | one per signal | Why the classifier decided `merger` over `tender_offer`, term by term |
| `extracted_fields` | **one per field per layer per run** | Every extracted value with its provenance and control outcomes |
| `extraction_runs` | one per layer per run | Token counts, chunk count, model id and prompt version for what a run cost |
| `runs` | one per run | Run registry with ingestion and schema versions |

Four decisions in there are load-bearing:

**`extracted_fields` is keyed on the layer, not just the field.** The unique
constraint is `(document_id, field_name, document_layer, run_id)`, so the same
field read from the filing summary and from the agreement is deliberately two
rows. Collapsing them would destroy the WS3 comparison before it ever ran.
This is also why ~98 rows appear for a 48-field merger, which reads as
duplication until you notice the `document_layer` column.

**Page text is stored.** A few hundred kilobytes per filing buys two things
WS8 needs: an evidence quote can be re-verified against its cited page without
re-parsing the PDF, and a citation stays resolvable when the source file is no
longer to hand. It is also what makes the locator URIs resolve against the
database rather than the filesystem — nothing in `deallens/` opens a file.

**Checksum is the deduplication key**, under a unique index. `document_id` is
derived from it (`doc_<sha256[:12]>`), so the same filing uploaded under a
different name is recognised as the same document, and its locators are
identical across machines.

**Re-running a run replaces it; a new run sits alongside.** `save_extraction`
deletes and rewrites rows for its own `run_id` and leaves earlier runs intact,
which is what makes a before/after comparison across prompt versions possible.
`save_ingestion` updates the `documents` row in place rather than deleting it,
because `extracted_fields` holds a foreign key to it and ingestion does not
regenerate extractions.

Two things are deliberately **not** stored, both derived on demand: the WS3
comparison and the WS4 timeline. The review queue can change a field's status,
and a persisted comparison would be stale the moment it did.

Manual corrections write to `extracted_fields` like any other value —
`extraction_method` becomes `manual` or `hybrid`, and the model's superseded
reading is kept in `notes`.

---

## Key Decisions

- **SQLite in-memory over ChromaDB/Pinecone**: Simpler, no persistence issues on Streamlit Cloud, sufficient for structured field Q&A
- **Text over page images where the filing allows it**: the original design sent the whole PDF as base64. Sending extracted text for machine-readable filings, and reserving page images for pages that genuinely need them, cut extraction input from 290,717 to 133,128 tokens for the same document (127,356 since prompt 3.0.0 moved the field descriptions out of the schema)
- **Per-session DB in st.session_state**: Isolates users, no cross-contamination
- **Synthetic assumptions clearly labeled**: All hedging inputs flagged as synthetic unless extracted from document

---

## Testing strategy

**233 tests, no network, no API key, no spend.** The model is replaced by a
fake that returns whatever payload a test specifies, and documents are built
by a synthetic PDF factory. The suite runs in about 40 seconds, which is the
point: a slow suite is a suite that stops being run. Captured output is in
`TEST_RESULTS.txt`.

| File | Tests | What it holds down |
|---|---:|---|
| `test_extraction.py` | 41 | Page mapping back to the source PDF, the four fail-closed controls, the output schema's shape, model selection and provenance, token-budget chunking, incomplete-source handling |
| `test_ingestion.py` | 39 | Page inventory, text-layer classification, duplicate and unreadable page detection, printed-label reconciliation, layer segmentation, locators |
| `test_comparison.py` | 32 | The seven WS3 classes, narrative vs typed comparison, the source hierarchy, that a conflicting value is never discarded |
| `test_hedging.py` | 28 | The required scenario × strategy grid, sign convention, what each hedge does *not* cover, FX from extracted currencies, probability weighting |
| `test_qa.py` | 25 | Answering only from asserted fields, the unsupported-answer sentence, refusal and truncation handling, prompt-injection fencing |
| `test_timeline.py` | 21 | The six date kinds, calculated dates and their derivations, refusing to place undatable entries, the hedge horizon |
| `test_review.py` | 18 | Manual correction and disclosure, normalization of reviewer input, conflicts reaching the queue |
| `test_generalization.py` | 17 | Document shapes and transaction structures beyond the development filing, financing-exhibit recognition |
| `test_persistence.py` | 12 | Database round-trip, re-ingestion, the audit record |

Three principles shape what is tested:

**Tests assert behaviour that would be wrong, not behaviour that exists.**
Most test names are claims — `test_a_value_that_will_not_normalize_is_refused_not_stored`,
`test_no_rate_hedge_covers_the_issuers_own_credit_spread`. Several were
written from real output after a defect appeared, and carry the case verbatim.

**The controls are tested from the failing side.** It is easy to prove a
clean extraction works. The tests that matter prove that a quote absent from
its page withholds the value, that an unparseable reviewer entry is refused
rather than stored, and that a question with no evidence returns the required
sentence without calling the API.

**Characterisation figures are re-measured, not assumed.** The offline cost
estimate is checked against the API's own `count_tokens` for the development
filing, with the measured figure stated in the test and flagged as a property
of the current prompt version rather than a constant.

Not covered: the Streamlit layer has no tests — it is exercised by hand — and
no test makes a live API call, so request-shape regressions surface only on a
real run.

---

## Risks

| Risk | Mitigation |
|------|-----------|
| A value is asserted that the document does not support | Fail closed on four independent grounds: normalization ambiguous, evidence quote absent from the cited page, no page resolved, or confidence below threshold. Any one withholds the value and routes the field to review |
| The model invents or omits a field | The response schema constrains every record's shape and confines `field_name` to an enum of the requested fields. Completeness is then checked in `extractor.py`: a field with no record is reported `unresolved`, not treated as absent |
| Two layers disagree and one reading is silently lost | Layers are extracted separately and never merged. Disagreement is classified as a conflict, both readings and both locators are kept, and the documented hierarchy nominates a value without discarding the other |
| A run costs more than expected | Priced before the first request from the page inventory, gated against a ceiling, and refused outright above it. Chunking is by token budget, not page count, because page size varies more than 20x within one filing |
| Document text carries an injected instruction | Untrusted content is fenced and declared as data in both extraction and Q&A, with the operator rules in the system prompt. Every value still has to survive evidence verification, which an invented value cannot |
| A citation drifts off its evidence | Locators carry a content anchor — a hash of the normalized quote — checked against stored page text. Page text lives in the database, so this works without the original PDF |
| A scanned or partially-scanned filing extracts as if complete | Unreadable pages block extraction rather than being skipped. Degraded text falls back to page images instead of being trusted |
| Streamlit session reset loses extracted data | By design on a deployment, where per-session isolation is the point. `DEALLENS_DB` gives a file-backed database for local work; the sidebar reports which mode is active |
| A cheaper model is used and the output misattributed | The model that ran is recorded on the run and on every field it produced. Pricing, and the spend gate, follow the model selected |

### Known limitations

- Q&A can only answer what the 50-field registry covers. A question about
  something outside it correctly returns "insufficient source support" even
  where the agreement addresses it — the trade for never answering from raw text
- The hedging analytics use one supplied DV01 for benchmark, credit-spread and
  swap-spread moves. Defensible at seven years, stated in the assumptions, and
  worth re-measuring before the figures are used to trade
- FX exposure is reported rather than priced: no FX rate or volatility is
  supplied, and inventing one would undo the point of the rest of the design
