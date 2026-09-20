# DealLens — Technical Memo

**AI-assisted transaction and hedging intelligence.** One pipeline reads a
public transaction filing, extracts 50 evidence-linked fields, compares what
the filing summary says against what the agreement says, builds a timeline,
and prices a hedge against it. Three filings have been run: a US cash merger
as the development case, and — without transaction-specific code — a second US
merger and a cross-border German takeover offer.

---

## 1. The chain, end to end

```
PDF ─> ingestion ─> extraction ─> extracted_fields ─┬─> summary vs. agreement
      (local, free)   (one API                       ├─> timeline & risk map
                       call per                      ├─> hedging scenarios
                       layer)                        ├─> grounded Q&A
                                                     └─> review & correction
```

Extraction writes to `extracted_fields`. Every analysis reads from
`extracted_fields` and derives from it rather than re-extracting, and a
reviewer's manual correction writes back to `extracted_fields`, so a
correction reaches all five consumers.

**Ingestion is not preparation — it is targeting.** It reads every page,
finds exhibit boundaries, identifies what instrument each one is, and only
then decides which layers a question can be asked of. On the German filing
that means sending 126 of 149 pages: the press release and investor deck are
withheld, not because they are unreadable, but because a press release cannot
tell you what the contract says. All of this is local; no API call happens
until the targeting is settled.

**Layers are never merged.** The same field read from the filing summary and
from the agreement is stored as two rows, deliberately. The comparison between
them is a deliverable, so collapsing them would destroy it before it could be
reported.

---

## 2. Controls: what has to be true before a value is asserted

A value reaches an answer only after surviving four independent checks:

| Control | Fails closed when |
|---|---|
| Normalization | "the first anniversary of the date hereof" will not resolve to a calendar date |
| Evidence verification | the quote does not appear on the page it cites |
| Page resolution | no source page resolved at all |
| Confidence threshold | below 0.60, or 0.75 for the 12 fields the assignment forbids inferring |

Failing any one withholds the **value** and keeps the **evidence**: the raw
text, the page and the quote are preserved and the field is routed to a human.
Across the three filings, 161 values were asserted and 47 withheld.

Two design choices carry most of the weight:

**Citations are content-anchored.** Every value carries a locator holding both
page numbers, the layer, and a hash of the normalized evidence quote, so a
citation that drifts off its evidence is detectable rather than merely
unlikely. Page text is stored, so a citation stays verifiable without the
original PDF.

**Nothing derived is stored.** The comparison and timeline are computed on
demand. A reviewer can change a field's status, and a persisted comparison
would be stale the moment they did.

---

## 3. Financial reasoning

The hedging analysis prices seven scenarios against three strategies and
attributes every result across the seven required risks. The separation is the
point, and it produces results a single net-P&L column would hide:

- **A hedge is not free.** Rates −25bp leaves the unhedged issuer **+$65mm**
  and the hedged issuer flat. The hedge gives up the upside with the downside.
- **Neither hedge covers issuer credit spread.** The combined scenario costs
  all three strategies the same **−$52mm**. A rate hedge neutralises the
  benchmark and converts it into swap-spread basis; it does nothing about the
  issuer's own spread.
- **On failure the structures diverge.** The conventional swap outlives the
  deal and must be unwound; the deal-contingent hedge terminates at no
  breakage cost, which is the reason it exists — and it charges a premium in
  every scenario, including the ones where the contingency is never used.

**Timing is not assumed.** The two delay scenarios take their dates from the
timeline, which derives them from the agreement's own extension clause and
shows the arithmetic. Where no extension can be parsed, no delay is assumed
and the scenarios report as unavailable rather than being given an invented
length.

Inputs the assignment does not supply — the size of the unspecified parallel
rate move, the deal-contingent premium, the unwind bid-offer — are in a
versioned `ADDITIONAL_ASSUMPTIONS` block, each with the reason it is required,
and are shown in the interface alongside the results. FX exposure is read from
the deal's extracted currencies rather than assumed, which is why the German
deal reports it and the two US deals do not.

---

## 4. Generalization

The validation filings were run against the same build with no per-deal
branching. Full analysis in `WS7_GENERALIZATION.md`.

| | Development | Validation 1 | Validation 2 |
|---|---|---|---|
| Structure classified | `merger` 1.00 | `merger` 0.73 | `takeover_offer` 0.87 |
| Values asserted | 53 | 50 | 58 |
| Cross-layer conflicts | 0 | 0 | 2 |

**What generalized.** The German takeover was classified unprompted, on
evidence the classifier found itself — `Wertpapiererwerbs`, `BaFin`,
`acceptance period`. Structure-aware narrowing reported
`shareholder_approval_threshold` as *not applicable* for it while reporting
the acceptance threshold instead, and the reverse for both mergers. EUR
resolved without configuration and flowed through to FX exposure.

**What needed extending — one thing, and it was real.** The German filing
attaches its bridge facility as an 84-page credit agreement. It matched no
instrument signature and the extractable-layer list covered only the summary
and the operative agreement, so the entire financing category had no source.
Nothing had failed: ingestion read all 84 pages and honestly reported an
unidentified exhibit. The gap was a whitelist drawn around the development
filing, whose only other exhibit is a two-page charter — against that one
document, "these exhibit kinds are not sources" and "nothing else is a source"
are indistinguishable. Adding a signature plus a category scope took financing
fields from 0 to **21** on that deal and changed nothing on the other two.

**What it found.** The only conflicts in the set are the same finding twice:
the filing summary describes a **EUR 14.2bn** bridge facility and the
agreement says **EUR 11.5bn**. Both readings are preserved with pages and
quotes; the agreement governs under the documented hierarchy; the field is in
the review queue.

---

## 5. Limitations

Stated plainly, because they bound what these outputs support.

- **"Asserted" means survived the controls, not correct.** No value has been
  checked against the filings by hand.
- **Evidence verification is the dominant failure mode and is over-strict.**
  All 47 withheld values failed it; classifying them shows 61% were the model
  abbreviating a long quote with `...` and 14% quotes spanning a page break.
  Only 20% were genuinely absent from the cited page. Handling elision and
  checking the adjacent page would recover most of the rest.
- **Three documents is a small sample**, two of them the same structure.
- **One page could not be read** — a flattened image on an investor slide. It
  is reported, and does not block extraction because it sits in a layer never
  opened. There is no OCR fallback.
- **Q&A can only answer what the 50-field registry covers**, which is the
  trade for never answering from raw text.
- **One DV01 does three jobs** — benchmark, credit-spread and swap-spread
  moves. Defensible at seven years, stated in the assumptions, worth
  re-measuring before the figures are traded on.

---

## 6. What I would do next

1. Accept elided quotes by verifying their segments, and check the cited page
   and its neighbour. Together these address 75% of withheld values without
   weakening the control.
2. Add OCR or a vision fallback for image-only pages.
3. Extend the instrument signatures as new exhibit kinds appear — guarantees,
   equity commitment letters, shareholder undertakings. The credit-agreement
   change is the template.
4. Cache Q&A answers by document and question; every ask is currently a fresh
   API call.
5. Re-measure classification confidence against more filings. A 0.73 on an
   ordinary US merger suggests narrow tuning, and three documents give no
   basis for a rejection threshold.

---

*24 modules, 242 tests, no network or API key required to run the suite.
Model `claude-opus-5`, prompt version `3.0.0`. Full plan and schema in
`EXECUTION_PLAN.md`; agent workflow and decision records in
`AGENT_WORKFLOW.md`; generalization analysis in `WS7_GENERALIZATION.md`.*
