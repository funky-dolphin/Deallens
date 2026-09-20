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

**What you start with.** The repository ships a sample database with three
filings already read and extracted, so the app has something in it the first
time you open it. You get your own private copy, and a refresh gives you a
fresh one — anything you upload or correct is yours alone and does not last.

**Keeping your own work.** If you want your uploads and corrections to
survive a refresh, add this to `.env`:

```
DEALLENS_DB=deallens.db
```

Then the app uses that one file and everything persists. Use this on your own
machine, not on a copy other people can reach.

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

## Hosting it

The app runs on [Streamlit Community Cloud](https://share.streamlit.io) with
no changes.

1. Push the repository to GitHub.
2. Create an app pointing at `app.py`, and choose **Python 3.11**.
3. Under **Settings → Secrets**, paste the contents of
   `.streamlit/secrets.toml.example` and fill in your API key.

Set no other configuration. In particular, **do not set `DEALLENS_DB`** on a
hosted copy — that points every visitor at one shared file, so they would see
each other's filings and each other's corrections.

**Reviewers arrive to finished work.** A sample database is committed
(`deallens_seed.db`) carrying three filings already read and extracted: two US
mergers and a German takeover offer. Each visitor gets their own private copy
of it, so there is something to look at immediately and nobody has to spend
money to see how it works.

They can still upload a filing of their own, extract it, and correct fields in
the review queue — all inside their own copy. Nothing they do affects anyone
else, and a refresh gives them a clean copy of the samples again.

**Your API key pays for any extraction a visitor runs.** Reading a filing is
free, and the samples cost nothing because they are already done. But anyone
who uploads a new PDF can spend $2 to $4 of your credit. There is a per-run
ceiling in `app.py` (`MAX_COST_USD`), but no daily cap and no per-visitor
limit. If the link is going further than a handful of reviewers, lower that
ceiling or put the app behind a password.

**Refreshing the samples.** The seed is a snapshot. If you re-extract or
correct something locally and want reviewers to see it, copy your working
database over the seed and commit it:

```bash
cp deallens.db deallens_seed.db
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
deallens_seed.db        those three filings, ready to open in the app
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
