# Agent Decision Records

Nine records from building DealLens with Claude Code. The assignment asks for
at least one successful delegation, one rejected recommendation, one incorrect
agent assumption, one debugging episode, one context improvement, and one
revision triggered by a failed test. Each is marked below.

The useful ones are not the successes. Four of these describe the agent being
wrong in a way that did not announce itself, which is the failure mode this
whole codebase is built to guard against in the model reading a filing — and
which applies equally to the model writing the code.

---

## DR-1 — Structured-output schema rejected by the API
**Category: debugging and recovery episode**

**What happened.** Extraction failed on every layer with
`The compiled grammar is too large, which would cause performance issues`.
The schema requested 48 fields as named object properties.

**What was done.** Rather than guess at a fix, seven candidate schema shapes
were probed against the live API with a trivial document and a small token
ceiling, so a rejection cost nothing. The results were unambiguous:

| Shape | Result |
|---|---|
| 48 named properties | rejected |
| 24 named properties | rejected |
| 12 named properties | rejected |
| **8 named properties** | **compiles** |
| Grouped under 6 category objects | rejected |
| Hoisted into `$defs` / `$ref` | rejected — *"Schema is too complex"* |
| **Array of records with a `field_name` enum** | **compiles** |

**Why it mattered.** The intuitive fix — hoisting the repeated shape into
`$defs` — fails *earlier* than the original, and the limit bites between 8 and
12 properties, far below anything that would have been guessed. Reasoning from
first principles would have produced a wrong answer confidently.

**Outcome.** The response became a list of records with the field name
constrained to an enum. That gave up the schema-level guarantee of one record
per field, so the completeness check moved into `extractor.py`, where a missing
field is reported `unresolved` rather than silently absent. Verified end to
end against the live API for about $0.45 in probes.

---

## DR-2 — Normalized match collapsed into a plain match
**Category: revision triggered by a failed test**

**What happened.** A test asserting that `"$250 million"` and `"$250,000,000"`
should classify as a *normalized match* failed. Both normalize to the same
float, and the comparison was checking normalized values, so it returned a
plain `match`.

**Why it mattered.** The assignment names `match` and `normalized match` as
distinct classes. Collapsing them would have hidden that the two documents
state the same term differently — which is exactly the kind of difference
Workstream 3 exists to surface.

**Outcome.** The distinction moved onto the source text: identical wording is
a `match`, the same value written differently is a `normalized match`. The
test was written before the implementation and caught the error immediately;
had it been written after, the behaviour would have looked correct.

---

## DR-3 — Extractable layers whitelisted around one document
**Category: incorrect agent assumption**

**What happened.** `EXTRACTABLE_LAYERS = ("filing-summary", "agreement")`, with
a comment reading *"Constitutional documents and press releases are not
sources of deal terms."* Correct for the development filing, whose only other
exhibit is a two-page charter.

**What it cost.** The second validation filing attaches its bridge facility as
an 84-page credit agreement. It matched no instrument signature, fell through
to a generic exhibit, and was never opened — so the entire financing category
had no source, and the filing priced *below* a shorter one because it was
costing 43 of 149 pages.

**Why it mattered.** Nothing failed. Ingestion read all 84 pages, stored their
text and honestly reported an unidentified exhibit. The defect was a
generalization from a single document — *"these two exhibit kinds are not
sources"* becoming *"nothing else is a source"* — and against that document
the two are indistinguishable.

**Outcome.** A `credit-agreement` instrument signature plus a category scope,
neither specific to the deal that exposed them. Financing fields on that
filing went from 0 to 21; the other two were unaffected. The assumption was
only falsifiable by a document the agent had not seen.

---

## DR-4 — Model selector over-built
**Category: rejected recommendation**

**What was asked.** "Can we add a toggle to switch between Claude models when
running the extraction."

**What was built.** A `ModelProfile` dataclass carrying context window,
pricing, thinking configuration and effort support; three models including one
with an incompatible request shape; per-model chunking; a CLI argument on the
cost estimator; and six tests.

**The correction.** *"Dont over complicate this. I just wanted the option to
select other claude models!"* and then *"Keep it simple!"*

**Outcome.** Cut to two models that share a request shape, so the selector is
a dropdown and a price lookup. Two things were kept and argued for: pricing
per model, because the estimate gates spend and pricing Sonnet at Opus rates
would block affordable runs; and the model id stamped on every field, because
it was previously hardcoded and a Sonnet run would have claimed Opus produced
it.

**What was learned.** The over-build came from solving the general problem
(any model) rather than the asked one (these models). Including Haiku — whose
thinking and effort parameters differ — was what dragged in the branching.

---

## DR-5 — Nine of ten conflicts were false
**Category: context improvement from human review**

**What happened.** The observation was *"I feel like there a lot of things are
marked as conflicting when they kinda say the same thing?"*

**What the data showed.** Ten conflicts on the German filing. One was
material — a bridge facility described as EUR 14.2bn in the summary and EUR
11.5bn in the agreement. The other nine were the filing summary summarising:

> summary: "the receipt of specified financial services regulatory approvals"
> agreement: "Monetary Authority of Singapore approval under Art. 28 …; Bank
> of Greece; Central Bank of Turkey approvals"

**Why it mattered.** A nine-to-one false positive rate buries the one finding
that matters. Comparing narrative text verbatim is a category error: a summary
is *expected* to differ in wording from the contract it summarises.

**Outcome.** Narrative and typed fields are now compared differently. Typed
values normalize to canonical forms where a difference is a disagreement;
narrative values conflict only when monetary amounts stated inside them
disagree. Conflicts went from 10 to 2, and both survivors are the real
finding. Nine tests were built from the actual values rather than invented
ones.

**What was learned.** The diagnosis in the prompt was wrong — this was not a
bug in the conflict logic — but the symptom was right. Naming a symptom
without prescribing a fix left room to find the actual cause.

---

## DR-6 — Audit record under-reported every run
**Category: incorrect agent assumption, found during review**

**What happened.** A routine check of stored token counts showed a 92-page
agreement recorded as 2,137 input tokens — implausible — and two different
documents recorded as *identical* 4,269 input tokens, which is effectively
impossible.

**The cause.** The document block is cached, so on a cold run almost the
entire input is billed as `cache_creation_input_tokens`. `ExtractionResponse`
captured it; `LayerExtraction` had no field for it, so it was dropped before
ever reaching the database.

**Why it mattered.** Workstream 8 requires a reproducible, auditable record of
what a run cost. Every run under-reported its own input volume, and the cost
figures derived from it were wrong.

**Outcome.** The field is carried through, a column added with a migration for
existing databases, and the interface now shows billed input including cache
creation. Three runs predating the column record `0` and cannot be recovered;
that is stated rather than back-filled with an estimate.

---

## DR-7 — Timeline, hedging and review built from a stated design
**Category: successful delegation**

**What was delegated.** Workstreams 4, 5 and 8's review queue, each from a
design agreed in conversation before any code: six date kinds with a separate
`extracted`/`calculated` axis; seven scenarios by three strategies with
seven-way risk attribution; manual correction normalized by the same code as a
model value.

**What made it work.** The design was settled first, including the awkward
parts — that most of a merger agreement's schedule is not a calendar date,
that a credit agreement should not be asked merger questions. Ambiguities were
raised as questions rather than resolved silently: whether to calculate
extension dates at all, and whether Workstream 5 should re-derive a close date
or consume Workstream 4's.

**Outcome.** All three landed with tests, and the test suite caught an error
the design review had not: the outcome probabilities summed to 1.10, because
the single supplied delayed-close probability was being applied to both delay
scenarios.

---

## DR-8 — A decision recorded in the documentation had been reversed
**Category: error in the written record**

**What happened.** `AGENT_WORKFLOW.md` recorded *"Send PDF directly to Claude
API as base64 document"* as a chosen approach. The code does the opposite, and
has since Workstream 2 — sending extracted text for machine-readable filings,
which is where the 54% input reduction came from.

**Why it mattered.** A design document that describes the opposite of the code
is worse than none: a reviewer either believes it and is misled, or finds the
discrepancy and distrusts the rest. Five further stale claims were found in
the same pass, including a database table that had never existed.

**Outcome.** The record is kept and marked **reversed**, with the reasoning and
the measurement. A decision that was later overturned is evidence; deleting it
would have hidden the reasoning that produced the improvement.

---

## DR-9 — Whole-document OCR block refused an extractable filing
**Category: incorrect agent assumption, corrected by a human decision**

**What happened.** One page in 149 — a flattened image on an investor slide —
set the document status to `blocked`, and the extraction gate refused the
whole filing. The page sits in a layer extraction never opens.

**The decision.** The initial human call was to leave it and record it as a
finding: *"I actually think we leave it and make a note of it."* On seeing
that 148 of 149 pages were readable, the call changed to fixing it — but with
a constraint the agent had not proposed: extract everything except the page
that does not work, rather than refuse.

**Outcome.** The gate now asks whether any page *in the layers this run reads*
is unreadable. Where one is, extraction still proceeds and every absence from
that layer is qualified — *"Not found in the readable part of this layer"* —
because "not found" and "not found, and we could not read everything" are
different claims. The control was made more precise rather than weaker.

**What was learned.** The agent's first instinct was to preserve the control
as written; the human's was to preserve the user's ability to work. Both were
partly right, and the resolution — narrow the control's scope, qualify what it
can no longer guarantee — came from the disagreement rather than from either
position.
