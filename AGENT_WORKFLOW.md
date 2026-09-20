# DealLens — Agent Workflow & Design Decisions

## Agent Used
Claude Code (CLI), running Claude Opus 5 — architecture, implementation,
test design, debugging and documentation, across the sessions logged below.

The application itself calls `claude-opus-5` by default for extraction and
for Q&A; `claude-sonnet-5` is selectable on the Extract page. Whichever
model runs is recorded on the run and on every field it produces, so an
output can be traced to the model that made it.

---

## What each part of the application is actually for

Each workstream has an obvious reading and a more useful one. The obvious
reading is what it does; the useful one is what it is protecting against.

**WS1 — Ingestion** is not preparing the document for the model. It is
deciding what the model is allowed to see. It reads every page, finds the
exhibit boundaries, works out what instrument each one is, and only then
decides which of them a question can be asked of. All of that is local and
free; no API call happens until the targeting is settled. On the Uber filing
it sends 126 of 149 pages — the press release and the investor deck are
withheld not because they are unreadable but because a press release cannot
tell you what the contract says.

**WS2 — Extraction** does not produce answers. It produces claims, each of
which has to survive four independent controls before it is allowed to become
an answer: it must normalize unambiguously, its quote must be found on the
page it cites, a page must have resolved at all, and its confidence must clear
a bar that is higher for fields the assignment forbids inferring. A claim that
fails any one of them is kept, with its raw text, and routed to a human — the
value is withheld, never the evidence.

**WS3 — Comparison** does not resolve a disagreement. It reports one. When
the 8-K says "$250 million" and the agreement says "$250,000,000", that is a
normalized match and worth knowing; when they say different things, that is
the deliverable. An earlier version merged layers on highest-confidence-wins
and would have destroyed the only evidence that the two documents differ,
while looking more confident for doing it.

**WS4 — Timeline** is mostly an exercise in restraint. Of the nine things it
must cover, two are calendar dates; the rest are anchored to events that have
not happened, conditioned on outcomes nobody knows, or frankly non-binding.
Its value is in refusing to place "30 days after written notice" anywhere on a
calendar, while still calculating the dates that genuinely follow from the
agreement's own extension clause — and showing the arithmetic when it does.

**WS5 — Hedging** is not asking what a hedge costs. It is asking which risks
a hedge leaves you holding, and the answer is most of them. Separating the
seven exposures is what makes that visible: a rate hedge neutralises the
benchmark, converts it into swap-spread basis, and does nothing whatever about
the issuer's own credit spread — so the credit scenario costs all three
strategies the same. A single net P&L column would have hidden that.

**WS6 — Q&A** never reads the filing. It reads what survived. Every control
WS2 applied is therefore still in force at the moment of answering, and a
question with no asserted evidence behind it returns the unsupported-answer
sentence without an API call. The cost is real and stated: it can only answer
what the 50-field registry covers.

**WS7 — Generalization** is not a test of whether the pipeline works. It is a
test of which assumptions were only ever true of the document they were
written against. The extractable-layer whitelist looked correct for as long as
the development filing was the only one in hand, and was falsified the first
time a filing attached a credit agreement as an exhibit. That is the finding;
the fix is the easy part.

**WS8 — Controls** all answer one question: what would make this output wrong,
and would anyone notice? Hence a locator that carries a content hash so a
citation cannot drift off its evidence unnoticed, a model id recorded per
field so a cheap run cannot be mistaken for an expensive one, and a manual
correction that preserves the reading it superseded. A control that fails
silently is worse than no control, because it also removes the suspicion that
would have caught the error.

---

## Workflow Log

### Session 1 — Architecture Design
**Decision**: Use in-memory SQLite over ChromaDB for document storage
- **Considered**: ChromaDB + Pinecone for vector similarity Q&A
- **Rejected because**: Streamlit Cloud ephemeral filesystem would wipe ChromaDB on restart; Pinecone adds cost and API key complexity
- **Chosen**: SQLite `:memory:` stored in `st.session_state` — per-session isolation, zero persistence issues, sufficient for structured field lookup

**Decision**: Send the PDF to the API as a base64 document block
- **Considered**: extracting text locally first
- **Rejected because**: adds dependencies, loses layout context
- **Chosen**: `{"type": "document", "source": {"type": "base64", ...}}`

> **Reversed in WS2.** A PDF block is billed as extracted text *and* a rendered
> image of every page — roughly 2,700 tokens per page against 1,200 for the
> same content as text. Ingestion already extracts and verifies the text, so
> paying to have the pages rendered and read again buys nothing on a
> machine-readable filing. Text is now sent wherever ingestion finds the text
> layer trustworthy, and page images are reserved for pages that genuinely
> need them. This cut input by 54%. The original reasoning was sound on its
> own terms and was made before there was a local text layer worth trusting.

**Decision**: Q&A via SQLite context injection, not RAG
- **Considered**: Embedding chunks into ChromaDB, similarity search on questions
- **Rejected because**: over-engineered for a bounded field set (now 50); the extracted data is already compact and grounded
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
1. Hold the model to an **output schema** — a list of records, each naming its
   field from a fixed enum, so an omitted or invented field is detected per
   field rather than corrupting the whole response
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

The list shape is forced on us rather than chosen. A structured-output schema
is compiled into a grammar, and an object with one required property per field
exceeds the API's grammar size limit between 8 and 12 properties — measured,
not inferred. With 48 fields in play the object form returns
`The compiled grammar is too large`, and neither grouping the fields under
category objects nor hoisting the repeated shape into `$defs` avoids it. Worth
knowing before anyone tries to key the schema by field name again; see the
module docstring in `prompts.py`.

---

## Hedging Model Design

The hedging module runs the seven required scenarios against the three
required strategies, and attributes every result across the seven risks the
assignment requires be kept separate.

| Scenario | Source of its inputs |
|---|---|
| Rates +25bp / -25bp / +50bp | Assignment |
| Parallel rate move + credit spread +20bp | Assignment; the rate move's size is assumed and stated |
| Closing delayed to first extension date | **Dated from the WS4 timeline** |
| Closing delayed to final extension date | **Dated from the WS4 timeline** |
| Transaction failure | Assignment |

| Risk reported separately | Who bears it |
|---|---|
| Benchmark rate | Unhedged only; both hedges neutralise it |
| Swap spread | Hedgers only — the basis a rate hedge leaves behind |
| Issuer credit spread | Everyone, in full. No rate hedge touches it |
| FX | Read from the deal's extracted currencies, not assumed |
| Timing | The conventional hedge, as carry across a delay |
| Completion | The deal-contingent premium, paid in every scenario |
| Unwind / breakage | The conventional hedge, on failure |

**Sign convention.** Positive is a gain to the issuer. The coupon is not yet
fixed, so a rise in rates raises the future cost and is a loss.

    DV01       = (notional / 100mm) x benchmark_DV01_per_100mm
    rate P&L   = -DV01 x rate_shift_bps
    carry      = notional x (swap_rate - treasury_rate) x delay_days / 365

Delay days come from the timeline rather than being derived here: two modules
computing a close date from the same agreement is two chances to disagree
about it.

Inputs the assignment does not supply live in `ADDITIONAL_ASSUMPTIONS`, each
with the reason it is required — the unspecified size of the parallel rate
move, the deal-contingent premium, the unwind bid-offer, and the use of one
supplied DV01 for credit-spread and swap-spread moves as well as benchmark
ones.

---

---

## Representative instructions and project context supplied

The assignment PDF was the source of truth throughout, read directly rather
than summarised, and re-read before each workstream. Standing context given at
the outset and restated when it slipped: fail closed rather than guess, cite
everything, never merge the document layers, and treat the filing as untrusted
input.

Instructions were mostly short and corrective rather than long and
specifying. A representative sample, verbatim:

> "Why isn't anything being posted to the extracted_fields table? Is that
> intended?"

> "I actually think we leave it and make a note of it. I think that if we can
> extract data from 147/148 pages is really good. And we have a case to make
> about whats next to come for this application."

> "Dont over complicate this. I just wanted the option to select other claude
> models!"

> "I feel like there a lot of things are marked as conflicting when they kinda
> say the same thing?"

> "Wait why are we calling the one table that we write to 'the hub'. Please
> just use the actual name of the table."

> "are we able to extract text from them?"

Two patterns in that sample did most of the work. **Questions rather than
instructions** — "is that intended?", "are we able to?" — consistently
surfaced defects, because they forced the answer to be checked rather than
asserted. And **naming the symptom without prescribing the fix** left room for
the diagnosis to be wrong, which it sometimes was: the "conflicts" observation
turned out to be a false-positive rate, not a bug in the conflict logic.

---

## Tests and validation loops

242 tests, no network and no API key required, running in about 40 seconds.
Captured output in `TEST_RESULTS.txt`; the strategy and per-file breakdown are
in `EXECUTION_PLAN.md`.

The loop that mattered was narrow: **state the behaviour as a test before
implementing, run the suite after every change, and treat a failure as
information rather than an obstacle.** Three times a failing test was right
and the design was wrong:

- A test asserting `"$250 million"` and `"$250,000,000"` should be a
  *normalized match* failed, because the comparison was checking normalized
  values — where both are the same float. The distinction had to move onto the
  source text.
- A test asserting `"Bio-Techne"` against `"Bio-Techne Corporation"` was a
  conflict passed, then had to be **reversed** once real output showed it was
  the summary using a short form, not two companies.
- A demo of the WS5 grid showed the outcome probabilities summing to 1.10,
  because the single supplied delayed-close probability was being applied to
  both delay scenarios.

Validation beyond the suite came from three sources: probing the live API for
behaviour that could not be reasoned about, measuring figures rather than
estimating them (`count_tokens` for the cost baseline), and running the
pipeline end to end on documents it had never seen.

---

## Approximate time spent

**Roughly 20 hours across three sessions**, against the assignment's estimate
of 8–12. The first session built ingestion and extraction; the second built
the timeline, hedging, Q&A and review; the third was generalization, exports
and documentation.

The overrun went almost entirely into three things: the fail-closed controls
and the tests holding them down, correcting claims in the documentation that
had become false, and the defects found by running against unseen documents.
None of it went into the happy path, which was working early.

---

## Estimated net agent leverage

**High on volume, mixed on judgement, and the mix is the interesting part.**

24 modules and 8,856 lines of Python with 242 tests, written in about 20
hours. That volume is not achievable by hand in the time, and the tests in
particular — which are the reason the defects below were catchable — would
have been the first thing cut under time pressure.

Against that, the agent's unsupervised judgement was wrong often enough to
matter, and in ways that would not have announced themselves:

- It over-built a model selector into profiles, a CLI flag and six tests when
  a dropdown was asked for, and had to be told twice to cut it back.
- It wrote a whitelist of extractable layers that was correct for one document
  and silently wrong for another.
- It dropped cache-creation tokens from the audit record, making every run
  under-report its own cost.
- It invented a duration — "eighteen months" — in a document about rigour.

Every one of those was caught by a human asking a direct question, or by a
test, or by checking a number against the system that produced it. The honest
summary is that the leverage is real but conditional: it multiplies output,
and it multiplies the cost of not reading that output. The controls in this
codebase exist because the same failure mode — a confident answer that is
wrong and does not look wrong — applies to the agent writing it as much as to
the model extracting from a filing.

---

## Improvements in a second iteration

1. **Write the documentation last, or regenerate it.** Design documents
   written alongside the code accumulated claims that were true when written
   and false later — a table that never existed, a decision that had been
   reversed, a status vocabulary that had changed. Six such errors were found
   and corrected. Anything derived from code should be generated from it.
2. **Run against an unseen document much earlier.** Every significant
   generalization defect surfaced within an hour of the first validation
   filing. Held back, they all looked like design decisions.
3. **Measure before optimising the prompt.** The schema shape was rebuilt
   because of an API limit discovered by hitting it; probing first would have
   been cheaper than debugging after.
4. **Treat display formatting as part of the deliverable.** Page numbers
   rendering as `3.0000`, money as `14200000000.0` and a status reading
   `blocked` for a document that extracts fine were all noticed by a reader,
   not by a test.
5. **Decide the review model before building the queue.** Manual correction
   arrived late; marking a field verified had until then left its value null,
   so a reviewer's judgement was recorded and then ignored downstream.

## Known Gaps / TODO

- [x] WS3: Filing summary vs agreement comparison, on the **Summary vs.
      agreement** page. Computed on demand from the stored fields, not
      persisted — the review queue can change a field's status, which would
      leave a stored comparison stale
- [x] WS4: Timeline and risk map, on the **Timeline & risk map** page. Dates
      the agreement fixes, dates derived from its extension clause (labelled
      `calculated`, with the arithmetic shown), and dates it only describes,
      kept apart. The hedge horizon it produces is what WS5 dates its delay
      scenarios from
- [ ] **Q&A costs one API call per question, and nothing is cached.** Every
      press of **Ask** is a fresh request, including the twelve presets; the
      same question asked twice bills twice. The only free path is a document
      with no asserted fields, which returns the unsupported-answer sentence
      without calling the API. This matters on a public deployment, where
      every visitor's questions bill to the owner's key. Caching by
      `(document_id, question)` would make repeat asks free and is roughly ten
      lines — not done, and worth stating in the productionization roadmap
      rather than quietly fixing
- [ ] **The OCR block is right but too coarse — scope it to the layers being
      extracted.** Uber / Delivery Hero ingests 148 of its 149 pages cleanly.
      The one failure is page 148: slide 15 of the investor presentation in
      `exhibit-ex99.2`, a single flattened JPEG with no text layer. That is
      correctly flagged `image_only` — there is demonstrably content there and
      we cannot read a word of it. But `may_extract` then blocks the whole
      document, and the page sits in a layer extraction never opens, so a
      Business Combination Agreement is refused over a chart in a slide deck.
      The fix is to ask whether any page *in the extractable layers* is
      unreadable rather than any page at all, keeping an absolute block when
      the filing summary or the agreement is affected. Left in place for now
      on purpose: it loosens a fail-closed rule, and the finding is worth more
      than the patch. A vision-model or OCR fallback for `image_only` pages is
      the fuller answer and a natural next capability
- [ ] **`EXTRACTABLE_LAYERS` misses financing exhibits.** Uber's bridge
      facility terms live in `exhibit-ex10.1` (83 pages), which extraction
      does not open, so the entire financing category has no source for that
      deal. Extending the layer set is a real schema extension, not a
      configuration tweak, and it raises the run cost materially
- [ ] WS7: extraction and cross-transaction analysis for Organon and Uber;
      hedging assumptions not yet adapted per deal
- [ ] **No live extraction has been run against any of the three case
      filings.** Ingestion runs end to end on all three and each prices
      offline; the paid calls are the step still outstanding, and they block
      the machine-readable extraction outputs deliverable:

      | Filing | Pages | Layers | Structure | Estimate |
      |---|---|---|---|---|
      | development (US merger) | 99 | 3 | `merger` 1.00 | $1.64–2.89 |
      | validation 1 (US merger) | 109 | 4 | `merger` 0.73 | $1.74–2.99 |
      | validation 2 (German takeover) | 149 | 5 | `takeover_offer` 0.87 | $2.34–4.22 |
- [ ] Streamlit Cloud deployment not yet done

---

## Error Recovery Patterns

**Malformed or omitted field**: the schema fixes each record's shape and the
set of names a record may claim, and `extractor.py` checks that a record
arrived for every requested field — so a missing field is detected per field
rather than per response. It is recorded as `unresolved` and routed to review;
the rest of the layer is kept. A field answered twice is reconciled as
competing readings rather than resolved by arrival order. A layer that fails
outright is recorded as a warning on the run and the remaining layers still
extract.

**API failure**: Caught by generic `except Exception` in extractor. Error bubbled to UI.

**Withheld value**: a field that was found but failed normalization, evidence
verification or the confidence threshold keeps its raw value and evidence so a
human can adjudicate, but its normalized value is not asserted. It is routed to
the review queue. Q&A only ever sees fields whose status is `found`, so a
withheld value cannot be laundered into an answer.
