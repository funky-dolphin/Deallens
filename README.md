# DealLens

Reads a merger or takeover filing and turns it into structured, checkable
transaction data — then uses that data to price a hedge.

Every value it reports comes with the sentence it came from, the page that
sentence is on, and which document in the filing it was found in. Where it
cannot support a value that way, it says so instead of answering.

---

## What it does

A public filing is not one document. It is a company's own summary of the
deal, stapled to the contract itself, stapled to a press release, sometimes
stapled to a loan agreement. Those documents describe the same deal in
different words, and occasionally disagree.

DealLens reads the filing in five steps:

1. **Sorts the filing into its parts** and works out what each one is — the
   summary, the contract, a press release, a credit agreement.
2. **Reads only the parts that can answer a question.** A press release cannot
   tell you what the contract says, so it is not asked.
3. **Pulls out 50 deal terms** from each part separately — price per share,
   termination fees, closing conditions, financing, how employee share awards
   are treated — each with a supporting quote.
4. **Compares the summary against the contract**, term by term, and reports
   where they agree, where they phrase things differently, and where they
   genuinely disagree. Nothing is quietly resolved.
5. **Builds a timeline and prices a hedge** against the deal's own dates.

Anything it is not confident about goes to a review queue, where a person can
check it and correct it.

---

## Running it

You need Python 3.11 and an Anthropic API key.

```bash
python -m venv .venv
.venv/bin/pip install -r requirements.txt

echo "ANTHROPIC_API_KEY=sk-ant-..." > .env

.venv/bin/streamlit run app.py
```

The app opens in a browser. Upload a PDF on the first page, then use the
sidebar to work through it.

**Keeping your work between sessions.** By default everything lives in memory
and disappears when you refresh the page. That is deliberate — it means two
people using a shared copy never see each other's documents. If you are
working on your own machine and want your data to persist, add this to `.env`:

```
DEALLENS_DB=deallens.db
```

Then reading a filing once is enough; you can close the browser and come back.

---

## What it costs

Reading a filing is free. Only step 3 above — pulling out the deal terms —
sends anything to the API, and it happens behind its own button so it is never
a surprise. Every run is priced before it starts and refuses to begin if the
estimate is too high.

In practice a 100 to 150 page filing costs **$2 to $4**. You can check the
price of a document without spending anything:

```bash
.venv/bin/python scripts/estimate_cost.py "your-filing.pdf"
```

---

## The pages

| Page | What it is for |
|---|---|
| **Ingest & inspect** | Upload a filing. Shows what was found in it, and which parts will be read |
| **Extract** | Pulls out the deal terms. The only page that spends money |
| **Summary vs. agreement** | Where the company's summary and the contract agree, differ, or conflict |
| **Timeline & risk map** | Dates the contract fixes, dates derived from it, and dates it only describes |
| **Review queue** | Anything the system would not assert on its own. Correct it here |
| **Hedging analysis** | Seven scenarios against three strategies, with each risk shown separately |
| **Q&A** | Ask questions. Answers come only from what was extracted, with citations |
| **Export** | Everything as a spreadsheet, or the written documents as PDFs |
| **Technical memo** | The three-page summary, readable in the app |

---

## Reading about it

| Document | What is in it |
|---|---|
| `TECHNICAL_MEMO.md` | Three pages: how it works, what it found, what it cannot do |
| `DECISION_RECORDS.md` | Nine decisions made while building it, including the mistakes |
| `WS7_GENERALIZATION.md` | What happened when it was run against filings it had never seen |
| `EXECUTION_PLAN.md` | The full plan, database layout, and testing approach |
| `AGENT_WORKFLOW.md` | How it was built with an AI coding assistant, and what that cost |

All five are also downloadable as PDFs from the Export page.

---

## Testing

```bash
.venv/bin/python -m pytest tests/ -q
```

257 tests, about 45 seconds. They need no API key and make no network calls —
the model is replaced by a stand-in, and test documents are generated rather
than downloaded. A captured run is in `TEST_RESULTS.txt`.

---

## Layout

```
app.py                  the application
deallens/
  ingestion/            reading a PDF and working out what is in it
  extraction/           pulling out the deal terms
  comparison.py         summary against contract
  timeline.py           dates, and which kind each one is
  analytics/hedging.py  scenarios and risk
  qa.py                 answering questions from extracted data
  review.py             human corrections
  export.py             spreadsheets and JSON
  pdf.py                documents as PDFs
  db/                   the database, and what it records
scripts/                pricing a run, exporting outputs
tests/                  257 tests
outputs/                extracted data for the three sample filings
```

---

## One thing worth knowing

The system is built to **refuse rather than guess**. If a quote cannot be
found on the page it claims to be from, if a date does not resolve to a real
date, or if the model is not confident enough, the value is withheld and sent
for review — the supporting evidence is kept either way.

That means it reports fewer answers than it could. Across the three sample
filings it asserted 161 values and held back 47. The held-back ones are not
lost; they are in the review queue with the page and quote, waiting for
someone to decide.
