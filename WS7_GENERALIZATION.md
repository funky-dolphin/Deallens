# Workstream 7 — Out-of-Sample Generalization

One pipeline, three filings, no transaction-specific code. The development
filing was a US cash merger; the two validation filings were run against the
same build with no per-deal branching, configuration or hard-coded values.
This is a report on how the pipeline coped, not a comparison of deal terms.

Model `claude-opus-5`, prompt version `3.0.0`, all three runs.

---

## 1. Results at a glance

| | Development<br>(US merger) | Validation 1<br>(US merger) | Validation 2<br>(German takeover) |
|---|---|---|---|
| Pages | 99 | 109 | 149 |
| Structure classified | `merger` **1.00** | `merger` **0.73** | `takeover_offer` **0.87** |
| Layers segmented | 3 | 4 | 5 |
| Layers extracted | 2 | 2 | **3** |
| Applicable fields | 48 | 48 | 48 |
| Values asserted | **53** | **50** | **58** |
| Not found | 30 | 24 | 36 |
| Withheld by a control | 13 | 22 | 12 |
| Not applicable | 2 | 2 | 2 |
| Cross-layer conflicts | 0 | 0 | **2** |
| Review queue | 13 | 22 | 16 |
| Output tokens | 15,352 | 16,890 | 19,083 |

Values asserted are fields that survived every control: normalized cleanly,
evidence quote verified against the page it cites, confidence above threshold.

---

## 2. What generalized successfully

**Structure classification.** The German voluntary public takeover offer was
identified with no prompting and no new code, on evidence the classifier
found for itself: `voluntary public takeover offer`, `Wertpapiererwerbs` (the
German Takeover Act), `BaFin`, `acceptance period`. The two US mergers were
classified correctly from their own signatures.

**Structure-aware field narrowing.** `applies_to` did exactly what it was
built for, and the excluded fields differ by deal:

| Filing | Reported `not_applicable` |
|---|---|
| Both US mergers | `offer_or_acceptance_period`, `tender_acceptance_threshold` |
| German takeover | `merger_subsidiary`, `shareholder_approval_threshold` |

This is the distinction the whole registry design rests on. A German takeover
has no shareholder vote, so `shareholder_approval_threshold` is reported as
**not applicable** rather than as a failed extraction — and conversely the
mergers report no acceptance threshold. Four fields flip between structures;
the other 46 apply to all of them.

**Currency handling.** `consideration_currency` resolved `USD`, `USD`, `EUR`
without configuration. The euro reading now drives both the FX exposure and
the currency the hedging figures are reported in, so the German deal is
analysed in euro rather than silently treated as dollars (see §3).

**Layer segmentation and citation.** Every filing segmented into its
constituent instruments, and every asserted value carries a layer, a page and
a verified quote. The summary-vs-agreement comparison ran on all three.

**Cost and scale.** The largest filing cost roughly a quarter more in output
tokens than the smallest despite being 50% longer, because targeting keeps
the input proportional to the layers worth reading rather than to the file.

---

## 3. What required a schema extension

Two, and both were found by the validation filings rather than by review.
Neither fix is specific to the deal that exposed it.

**Schema extension #1: financing agreements attached as exhibits (applied).**

The German filing attaches its bridge facility as Exhibit 10.1 — a
`BRIDGE CREDIT AGREEMENT`, 84 pages, Morgan Stanley Senior Funding as
administrative agent. It matched no instrument signature, fell through to a
generic `exhibit`, and `EXTRACTABLE_LAYERS` covered only the filing summary
and the operative agreement. The entire financing category had no source, and
the filing priced *below* the development one despite being half again as
long, because it was costing 43 of 149 pages.

Nothing had failed. Ingestion segmented the layer, read all 84 pages, stored
their text and honestly reported an unidentified exhibit. The gap was a
whitelist drawn around the development filing, whose only other exhibit is a
two-page charter — against that document, "these exhibit kinds are not
sources" and "nothing else is a source" are indistinguishable.

Two modular changes, neither specific to the deal that exposed them:

- a `credit-agreement` instrument signature matching `BRIDGE CREDIT
  AGREEMENT`, `CREDIT AGREEMENT`, `FACILITIES AGREEMENT`, `COMMITMENT LETTER`
  and `INTERIM FACILITIES AGREEMENT`, ordered after the operative-agreement
  signature so a merger agreement still classifies as the agreement
- the layer added to `EXTRACTABLE_LAYERS` and scoped through
  `LAYER_FIELD_CATEGORIES` to the financing category only — a credit
  agreement has its own material adverse effect clause, conditions precedent
  and termination provisions, all about the loan, and asking it the full
  field set would answer merger questions from the wrong contract

**The effect is the single clearest result in this workstream:**

| Financing fields asserted | Development | Validation 1 | Validation 2 |
|---|---|---|---|
| Before the extension | 1 | 7 | — |
| After | 1 | 7 | **21** |

The German deal went from having no source for its bridge terms to asserting
`bridge_amount` (EUR 14,200,000,000), `bridge_currency` (EUR),
`bridge_maturity` (364 days after the Closing Date) and `interest_basis`
(EURIBOR plus an applicable margin). The two US filings are unchanged —
neither attaches a financing agreement.

**Schema extension #2: the analytics were not adapting to the deal (applied).**
Found by a reviewer noticing that changing the selected document left every
hedging figure unchanged.

It was not a display fault. The analysis took its notional, tenor, currency
and rate basis entirely from the assignment's standardized financing block, so
it priced a **USD 4bn seven-year fixed-rate issuance for all three filings** —
including one whose disclosed facility is **EUR 14.2bn over 364 days**, with
every one of those facts already extracted and sitting in `extracted_fields`.

The assignment is explicit: *"For the validation transactions, adapt the
analytics to the extracted transaction characteristics. If data are
unavailable, use clearly labeled synthetic assumptions."* The build was always
taking the second branch.

`resolve_financing` now prefers the filing and falls back to the assumption,
marking each input `extracted`, `assumed` or `derived`:

| Input | Development | Validation 1 | Validation 2 |
|---|---|---|---|
| Notional | 4,000,000,000 *assumed* | 4,000,000,000 *assumed* | **11,500,000,000 extracted** |
| Currency | USD *extracted* | USD *extracted* | **EUR extracted** |
| Tenor (years) | 7 *assumed* | 7 *assumed* | **0.997 extracted** |
| Rate basis | swap rate *assumed* | swap rate *assumed* | **EURIBOR + margin extracted** |
| DV01 *derived* | 2,600,000 | 2,600,000 | **1,064,654** |

The effect on the headline figures:

| Rates +25bp, unhedged | Development | Validation 1 | Validation 2 |
|---|---|---|---|
| Before | −65,000,000 | −65,000,000 | −65,000,000 |
| After | −65,000,000 USD | −65,000,000 USD | **−26,616,339 EUR** |

Two things worth stating about the result. The notional used is the
**agreement's** 11.5bn rather than the summary's 14.2bn, because the source
hierarchy says the executed contract governs — so the analysis and the
press-facing number differ, deliberately. And DV01 is scaled linearly by
tenor, a first-order duration approximation: crude, labelled `derived` with
its arithmetic, and far closer than ignoring tenor, which overstated a
364-day facility's rate sensitivity roughly sevenfold.

The two US mergers still price identically to each other. That is correct —
neither states a facility, so both fall back to the one supplied financing
block.

---

## 4. What failed or produced low confidence

### 4.1 The dominant failure mode is evidence verification, and it is
mostly mechanical

Across all three filings, **44 values were withheld, and 44 of 44 failed the
evidence check** — not the confidence threshold, not normalization. Every
withheld value had a quote that could not be matched to the page it cited.
Classifying those 44:

| Cause | Count | Share |
|---|---|---|
| Model elided the quote with `...` | 27 | 61% |
| Quote spans a page boundary | 6 | 14% |
| Truncated or paraphrased tail | 2 | 5% |
| **Genuinely not on the cited page** | **9** | **20%** |

So the control is working — it caught nine misattributed citations that would
otherwise have been asserted as fact, all in Validation 1. But it is
over-triggering roughly four to one on causes that are not fabrication:

- **Elision.** The model returns `"clause A ... clause B"` to abbreviate a
  long provision. That is not a verbatim quote, so the verifier correctly
  rejects it — but the underlying value is very likely sound.
- **Page boundaries.** A provision straddling two pages is quoted in full and
  cited to the page where it begins. The verifier checks only that page.

Both are addressable and neither requires weakening the control; see §7.

### 4.2 Classification confidence varies more than expected

Validation 1 classified as `merger` at **0.73** against the development
filing's **1.00**. The verdict is correct, but reached on thinner evidence,
and a threshold set naively at 0.8 would have rejected a perfectly ordinary
US merger. The confidence is reported rather than rounded up.

### 4.3 One filing could not be read in full

Validation 2 carries one image-only page in 149 — a single flattened JPEG on
slide 15 of the investor presentation, with a three-character text layer. It
is correctly flagged `ocr_required`. It does not stop the run, because it
falls in a layer extraction never opens; that distinction is itself a change
this workstream forced (§7).

### 4.4 A real discrepancy, surfaced

Validation 2 produced the only cross-layer conflicts in the set, and both are
the same finding: the filing summary describes a **EUR 14,200,000,000**
bridge facility and the agreement says **EUR 11,500,000,000**. Both readings
are preserved with their pages and quotes; the agreement governs under the
documented source hierarchy, and the field is in the review queue. This is
precisely the kind of gap Workstream 3 exists to surface, and it was found
only because the layers are never merged.

---

## 5. Differences in legal terminology and transaction structure

The same 50-field registry absorbed all three without new fields, because the
registry is written in terms of function rather than US merger vocabulary.
What the same field name resolved to:

| Field | US mergers | German takeover |
|---|---|---|
| Approval gate | `shareholder_approval_threshold` — "the affirmative vote of the holders of a majority of…" | `tender_acceptance_threshold` — "at least 50% of the number of Shares … plus one share" |
| Offer mechanics | not applicable | `offer_or_acceptance_period` — "an offer period (Section 16 para. 1 WpÜG) of…" |
| Operative contract | Agreement and Plan of Merger | **Business Combination Agreement** |
| Acquisition vehicle | Merger Sub | not applicable — no merger subsidiary exists |
| Consideration | USD 73.00 / USD 14.00 per share | **EUR 41.50** per share |
| Regulator | HSR Act, antitrust clearance | **BaFin**, Monetary Authority of Singapore, Bank of Greece, Central Bank of Turkey |
| Financing | no attached facility | **Bridge Credit Agreement**, EURIBOR-based |

Two observations worth drawing out:

**The operative-agreement signature already covered the German form.**
`BUSINESS COMBINATION AGREEMENT` was in the pattern alongside
`AGREEMENT AND PLAN OF MERGER`, so the contract was identified as the
operative agreement rather than a generic exhibit. Had it not been, the
failure would have been total rather than partial.

**Narrative fields differ in wording between summary and agreement far more
than typed fields do.** On Validation 2 the first comparison run reported ten
conflicts, of which one was material. The other nine were the filing summary
summarising — "the receipt of specified financial services regulatory
approvals" against a list naming three central banks. Comparing narrative
text verbatim produces a nine-to-one false positive rate, which buries the
real finding. The comparison now treats narrative and typed fields
differently; see §7.

---

## 6. Extraction accuracy and performance by document

| | Development | Validation 1 | Validation 2 |
|---|---|---|---|
| Asserted / applicable | 53 / 96 | 50 / 96 | 58 / 96 |
| Withheld by a control | 13 | 22 | 12 |
| Asserted, Transaction identity | 17 | 8 | 12 |
| Asserted, Conditions | 9 | 15 | 6 |
| Asserted, Termination | 11 | 8 | 11 |
| Asserted, Equity treatment | 10 | 8 | 3 |
| Asserted, Financing | 1 | 7 | **21** |
| Asserted, Timing | 5 | 4 | 5 |
| Dated timeline entries | 2 | 2 | 1 |
| Hedge horizon complete | yes (273 days) | yes (275 days) | **no** |
| Financing inputs taken from the filing | 1 of 4 | 1 of 4 | **4 of 4** |

Counts are out of 96 — 48 applicable fields across two compared layers.

**Equity treatment fell sharply on the German deal** (3 against 10 and 8).
A German takeover offer does not convert equity awards at an effective time
the way a US merger does; award treatment is handled through separate
undertakings, and much of it is genuinely absent from the two layers
compared. This is a finding about the transaction type, not a failure.

**The hedge horizon could not be built for Validation 2.** No outside date
resolved, because a German voluntary offer has no long-stop date in the form
a merger agreement states one — the acceptance period and statutory deadlines
do that work instead. The timeline reports this rather than inventing a date,
and Workstream 5's delay scenarios correctly report as unavailable rather
than assuming a delay length. It is the clearest example of the pipeline
declining to answer rather than guessing.

**Extension clauses parsed on none of the three.** No filing stated its
extension mechanics in a form the parser could quantify, so no calculated
dates were produced anywhere. The conservative default held — nothing was
fabricated — but it means the calculated-date path is unexercised on real
documents.

---

## 7. Recommended architecture improvements

Ordered by expected value.

**1. Accept elided quotes by verifying their segments.** 61% of all withheld
values failed only because the model abbreviated a long provision with `...`.
Splitting on the ellipsis and requiring every segment to appear on the cited
page preserves the control's strength while removing its largest false
positive class. Expected to recover roughly 27 of 44 withheld values across
these three filings.

**2. Verify quotes against the cited page and its neighbour.** A further 14%
span a page boundary. Checking page *n* and *n+1* addresses this without
loosening what counts as a match.

**3. Instruct the model not to elide.** Complementary to (1) and cheaper —
a prompt-level fix, though less robust than handling elision properly.

**4. Extend the extractable-layer set as new instrument kinds appear.**
The credit-agreement extension is the template: a signature plus a
category scope, not a special case. Likely next candidates are guarantees,
equity commitment letters and shareholder undertakings.

**5. Scope integrity blocks to the layers being read.** Already applied
during this workstream, and worth recording as a pattern: a document-level
control that ignores which layers a run touches will refuse work it has no
bearing on. Validation 2 was initially refused in full over one slide in an
investor deck.

**6. Adapt the analytics to the extracted deal.** Applied during this
workstream, and the pattern generalises past this one case: a synthetic input
should be a fallback, not a default. Anything the filing states should be
used and labelled as extracted, so a reader can see which figures rest on the
document and which on an assumption. The remaining assumed inputs — the rate
levels, the probabilities, the DV01 per 100mm — are genuinely not in a filing,
which is why they stay assumed.

**7. Treat narrative and typed fields differently in comparison.** Also
applied during this workstream. Typed values normalize to canonical forms
where a difference is a disagreement; narrative values are expected to differ
between a summary and the contract it summarises. Comparing both the same way
produced a nine-to-one false positive rate.

**8. Add OCR or a vision fallback for `image_only` pages.** Not needed for
these three, where the single affected page is in a layer never read, but it
is the only remaining path to a genuinely complete read of an arbitrary
filing.

**9. Re-measure classification confidence against more filings.** A 0.73 on
an ordinary US merger suggests the scoring is tuned narrowly. With only three
documents there is no basis for setting a rejection threshold.

---

## 8. Honest limitations of this analysis

- **Three documents is a small sample.** Two of the three are US mergers, so
  a single German takeover carries all the structural variation.
- **Accuracy here means "survived the controls", not "correct".** No value
  has been checked against the filings by hand. A field asserted with a
  verified quote can still be the wrong provision.
- **The 20% of evidence failures classed as "genuinely not on the page" were
  not individually reviewed.** They may include verifier limitations beyond
  the two identified.
- **Extension-clause parsing is unexercised**, so the calculated-date path in
  Workstream 4 has no real-document evidence behind it.
- **FX exposure is identified but still not priced.** The euro deal is now
  analysed in euro, on a euro notional, rather than being silently treated as
  dollars — but no FX rate or volatility is supplied, so the FX column remains
  an exposure rather than a number.
- **Tenor is read, duration is approximated.** DV01 scales linearly with the
  stated maturity against the supplied seven-year figure. Right order of
  magnitude, not a curve.
