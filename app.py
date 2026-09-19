"""
app.py
DealLens -- Streamlit front end over the controlled ingestion and extraction
pipeline.

The page order mirrors the order the controls run in, and the free stages are
deliberately separated from the paid one: a document is ingested, inspected
and priced before any request is sent, and extraction only proceeds when an
operator authorises it against a spend ceiling.
"""

from __future__ import annotations

import os
import uuid
from pathlib import Path

import pandas as pd
import streamlit as st
from dotenv import load_dotenv

from deallens.analytics.hedging import BIO_TECHNE_ASSUMPTIONS, run_scenarios
from deallens.db import (
    get_connection,
    get_document,
    get_documents,
    get_extracted_fields,
    get_integrity_issues,
    get_layers,
    get_review_queue,
    initialize_schema,
    save_extraction,
    save_ingestion,
    set_review_status,
)
from deallens.extraction import MODEL_ID, PROMPT_VERSION, extract_document, fields_for
from deallens.extraction.client import INPUT_USD_PER_MTOK, OUTPUT_USD_PER_MTOK
from deallens.extraction.extractor import estimate_run
from deallens.ingestion import ingest

load_dotenv()

REPO_ROOT = Path(__file__).resolve().parent

st.set_page_config(page_title="DealLens", page_icon="🔍", layout="wide")


# ── Session state ─────────────────────────────────────────────────────────────
# One in-memory database per browser session, so concurrent users cannot see
# one another's documents. The IngestionResult objects are held in session
# state too: the database records what ingestion found, but extraction needs
# the live object (page text, layer boundaries) and the original bytes.
if "db" not in st.session_state:
    st.session_state.db = initialize_schema(get_connection())
    st.session_state.ingestions = {}
    st.session_state.pdf_bytes = {}

conn = st.session_state.db


def _api_key() -> str:
    """Resolve the key from Streamlit secrets, then the environment."""
    try:
        if "ANTHROPIC_API_KEY" in st.secrets:
            return st.secrets["ANTHROPIC_API_KEY"]
    except Exception:  # no secrets.toml present -- normal when running locally
        pass
    return os.getenv("ANTHROPIC_API_KEY", "")


def _anthropic_client(api_key: str):
    import anthropic

    return anthropic.Anthropic(api_key=api_key)


def _doc_picker(label: str = "Document") -> str | None:
    """Select one persisted document; returns its document_id."""
    docs = get_documents(conn)
    if not docs:
        st.info("No documents ingested yet. Start on **Ingest & inspect**.")
        return None
    options = {f"{d['filename']}  ·  {d['document_id'][:12]}": d["document_id"] for d in docs}
    return options[st.selectbox(label, list(options.keys()))]


# ── Sidebar ───────────────────────────────────────────────────────────────────
with st.sidebar:
    st.title("🔍 DealLens")
    st.caption("AI-assisted transaction & hedging intelligence")
    st.divider()

    api_key = st.text_input(
        "Anthropic API key",
        type="password",
        value=_api_key(),
        help="Read from Streamlit secrets or .env if present. Never stored or logged.",
    )
    cost_ceiling = st.number_input(
        "Spend ceiling per run (USD)",
        min_value=0.50,
        max_value=100.0,
        value=8.0,
        step=0.50,
        help="Extraction refuses to start if the priced estimate exceeds this.",
    )

    st.divider()
    page = st.radio(
        "Navigation",
        [
            "1 · Ingest & inspect",
            "2 · Price & extract",
            "3 · Extracted fields",
            "4 · Review queue",
            "5 · Hedging analysis",
            "6 · Q&A",
        ],
    )
    st.divider()
    st.caption(f"model `{MODEL_ID}`")
    st.caption(f"prompt `{PROMPT_VERSION}`")


# ── 1 · Ingest & inspect ──────────────────────────────────────────────────────
if page.startswith("1"):
    st.title("Ingest & inspect")
    st.caption(
        "Ingestion reads, classifies and integrity-checks the document locally. "
        "No API request is sent and nothing is spent on this page."
    )

    samples = sorted(p.name for p in REPO_ROOT.glob("*.pdf"))
    source = st.radio("Source", ["Upload a PDF", "Use a PDF in the repo"], horizontal=True)

    pdf_bytes: bytes | None = None
    filename: str | None = None
    if source == "Upload a PDF":
        uploaded = st.file_uploader("Choose a PDF", type=["pdf"])
        if uploaded:
            pdf_bytes, filename = uploaded.read(), uploaded.name
    elif samples:
        choice = st.selectbox("File", samples)
        pdf_bytes, filename = (REPO_ROOT / choice).read_bytes(), choice
    else:
        st.info("No PDFs found in the repository root.")

    source_url = st.text_input("Source URL (optional)", placeholder="https://www.sec.gov/...")

    if pdf_bytes and st.button("Ingest document", type="primary"):
        run_id = f"ui-{uuid.uuid4().hex[:8]}"
        with st.spinner("Reading pages, segmenting layers, checking integrity…"):
            result = ingest(pdf_bytes, filename, run_id=run_id, source_url=source_url or None)
            save_ingestion(conn, result)
        st.session_state.ingestions[result.document_id] = result
        st.session_state.pdf_bytes[result.document_id] = pdf_bytes
        st.success(f"Ingested `{filename}` as `{result.document_id[:12]}` (run `{run_id}`)")

    document_id = _doc_picker("Inspect document")
    if document_id:
        doc = get_document(conn, document_id)
        status = doc["ingestion_status"]
        badge = {"ingested": "✅", "ingested_with_warnings": "⚠️", "review_required": "⚠️"}.get(
            status, "⛔"
        )

        c1, c2, c3, c4 = st.columns(4)
        c1.metric("Pages", doc["page_count"])
        c2.metric("Status", f"{badge} {status}")
        c3.metric("Structure", doc["transaction_structure"])
        c4.metric("Structure confidence", f"{doc['structure_confidence']:.0%}")

        if status == "blocked":
            st.error(
                "Extraction is blocked for this document: pages carry content that could "
                "not be read. Extracting anyway would look complete and silently omit them."
            )

        st.caption(
            f"checksum `{doc['checksum'][:16]}…` · {doc['byte_size']:,} bytes · "
            f"machine-readable: {bool(doc['is_machine_readable'])} · "
            f"ingested {doc['ingestion_timestamp']} · ingestion v{doc['ingestion_version']}"
        )

        st.subheader("Layers")
        layers = get_layers(conn, document_id)
        if layers:
            st.dataframe(
                pd.DataFrame(layers)[
                    [
                        "layer_id",
                        "label",
                        "exhibit_number",
                        "start_page",
                        "end_page",
                        "detection_method",
                    ]
                ],
                width="stretch",
                hide_index=True,
            )
        else:
            st.warning("No layers were segmented.")

        st.subheader("Integrity issues")
        issues = get_integrity_issues(conn, document_id)
        if issues:
            st.dataframe(
                pd.DataFrame(issues)[["severity", "kind", "pdf_pages", "detail"]],
                width="stretch",
                hide_index=True,
            )
        else:
            st.success("No integrity issues found.")


# ── 2 · Price & extract ───────────────────────────────────────────────────────
elif page.startswith("2"):
    st.title("Price & extract")
    st.caption(
        "The estimate below is computed locally and costs nothing. "
        "Only the **Run extraction** button spends money."
    )

    live = st.session_state.ingestions
    if not live:
        st.info(
            "No document is loaded in this session. Ingest one on **Ingest & inspect** first — "
            "extraction needs the in-session ingestion object and the original file bytes."
        )
    else:
        options = {
            f"{r.inventory.filename}  ·  {doc_id[:12]}": doc_id for doc_id, r in live.items()
        }
        document_id = options[st.selectbox("Document", list(options.keys()))]
        ingestion = live[document_id]
        pdf_bytes = st.session_state.pdf_bytes[document_id]

        specs = fields_for(ingestion.structure.structure)
        estimate = estimate_run(ingestion, specs)

        c1, c2, c3 = st.columns(3)
        c1.metric("Applicable fields", len(specs))
        c2.metric("Requests", estimate.requests)
        c3.metric("Estimated cost", f"${estimate.cost_low:.2f}–${estimate.cost_high:.2f}")

        if estimate.layers:
            st.dataframe(
                pd.DataFrame(
                    [
                        {
                            "layer": l.layer_id,
                            "pages": l.pages,
                            "input tokens": f"{l.input_tokens:,}",
                            "requests": l.requests,
                            "cost": f"${l.cost_low:.2f}–${l.cost_high:.2f}",
                        }
                        for l in estimate.layers
                    ]
                ),
                width="stretch",
                hide_index=True,
            )

        if not ingestion.may_extract:
            st.error(
                f"Extraction is blocked: ingestion status is "
                f"'{ingestion.integrity.ingestion_status}'. "
                f"{len(ingestion.integrity.unreadable_pages)} page(s) require OCR."
            )
        elif estimate.cost_high > cost_ceiling:
            st.warning(
                f"Estimated cost exceeds the ${cost_ceiling:.2f} ceiling set in the sidebar. "
                "Raise the ceiling to authorise this run."
            )

        st.divider()
        st.warning(
            f"**This sends {estimate.requests} request(s) to the Claude API and will be billed "
            f"to your key** — roughly ${estimate.cost_low:.2f}–${estimate.cost_high:.2f}."
        )

        if st.button("Run extraction", type="primary", disabled=not ingestion.may_extract):
            if not api_key:
                st.error("Enter an Anthropic API key in the sidebar.")
            else:
                spinner = f"Extracting {len(specs)} fields across {estimate.requests} request(s)…"
                with st.spinner(spinner):
                    run = extract_document(
                        _anthropic_client(api_key),
                        ingestion,
                        pdf_bytes,
                        max_cost_usd=cost_ceiling,
                    )

                if run.error:
                    st.error(run.error)
                else:
                    save_extraction(conn, run)
                    actual = (
                        run.total_input_tokens * INPUT_USD_PER_MTOK
                        + run.total_output_tokens * OUTPUT_USD_PER_MTOK
                    ) / 1_000_000
                    counts = run.by_status()
                    st.success(
                        f"Extracted {len(run.fields)} fields from {len(run.layers)} layer(s) "
                        f"· run `{run.run_id}`"
                    )
                    c1, c2, c3, c4 = st.columns(4)
                    c1.metric("Found", counts.get("found", 0))
                    c2.metric("Not found", counts.get("not_found", 0))
                    c3.metric(
                        "Conflict / unresolved",
                        counts.get("conflict", 0) + counts.get("unresolved", 0),
                    )
                    c4.metric("Actual cost", f"${actual:.2f}")
                    st.caption(
                        f"{run.total_input_tokens:,} input tokens "
                        f"({run.total_cache_read_tokens:,} read from cache) · "
                        f"{run.total_output_tokens:,} output tokens"
                    )

                if run.warnings:
                    with st.expander(f"Warnings ({len(run.warnings)})"):
                        for warning in run.warnings:
                            st.write(f"- {warning}")


# ── 3 · Extracted fields ──────────────────────────────────────────────────────
elif page.startswith("3"):
    st.title("Extracted fields")

    document_id = _doc_picker()
    if document_id:
        rows = get_extracted_fields(conn, document_id)
        if not rows:
            st.info("Nothing extracted for this document yet. See **Price & extract**.")
        else:
            show_empty = st.checkbox(
                "Show fields that were not found or do not apply", value=False
            )
            grouped: dict[str, list[dict]] = {}
            for row in rows:
                grouped.setdefault(row["category"] or "uncategorised", []).append(row)

            for category, items in grouped.items():
                visible = [
                    r
                    for r in items
                    if show_empty or r["status"] in {"found", "conflict", "unresolved"}
                ]
                if not visible:
                    continue
                with st.expander(f"**{category}** ({len(visible)})", expanded=True):
                    st.dataframe(
                        pd.DataFrame(
                            [
                                {
                                    "field": r["field_name"],
                                    "value": r["normalized_value"],
                                    "ccy": r["currency"],
                                    "status": r["status"],
                                    "conf": r["confidence"],
                                    "layer": r["document_layer"],
                                    "page": r["printed_page"] or r["pdf_page"],
                                    "evidence ✓": r["evidence_verified"],
                                    "review": r["review_status"],
                                    "evidence": r["evidence"],
                                }
                                for r in visible
                            ]
                        ),
                        width="stretch",
                        hide_index=True,
                    )


# ── 4 · Review queue ──────────────────────────────────────────────────────────
elif page.startswith("4"):
    st.title("Review queue")
    st.caption(
        "Fields the pipeline declined to assert: below the confidence threshold, "
        "unverifiable evidence, ambiguous normalization, or conflicting readings "
        "across layers. Critical fields first."
    )

    document_id = _doc_picker()
    if document_id:
        queue = get_review_queue(conn, document_id)
        if not queue:
            st.success("Nothing awaiting review for this document.")
        for row in queue:
            mark = "🔴" if row["is_critical"] else "🟡"
            with st.expander(f"{mark} **{row['field_name']}** — {row['status']}"):
                st.write(f"**Raw value:** {row['raw_value'] or '—'}")
                st.write(f"**Normalized:** {row['normalized_value'] or '—'}")
                st.write(
                    f"**Confidence:** {row['confidence']:.2f} · "
                    f"**Layer:** {row['document_layer']} · "
                    f"**Page:** {row['printed_page'] or row['pdf_page'] or '—'}"
                )
                if row["evidence"]:
                    st.write(f"**Evidence:** “{row['evidence']}”")
                    st.caption(
                        f"evidence verified against the cited page: {row['evidence_verified']}"
                    )
                for note in row["notes"]:
                    st.caption(f"· {note}")
                if row["locator_uri"]:
                    st.code(row["locator_uri"], language=None)

                note = st.text_input("Reviewer note", key=f"note-{row['id']}")
                c1, c2 = st.columns(2)
                if c1.button("Mark verified", key=f"ok-{row['id']}"):
                    set_review_status(conn, row["id"], "verified", note or None)
                    st.rerun()
                if c2.button("Keep as exception", key=f"ex-{row['id']}"):
                    set_review_status(conn, row["id"], "exception", note or None)
                    st.rerun()


# ── 5 · Hedging analysis ──────────────────────────────────────────────────────
elif page.startswith("5"):
    st.title("Hedging & financing analysis")
    st.caption("All market and financing inputs below are **synthetic assumptions**.")

    with st.expander("Assumptions", expanded=False):
        c1, c2, c3 = st.columns(3)
        c1.markdown("**Financing**")
        c1.json(BIO_TECHNE_ASSUMPTIONS["financing"])
        c2.markdown("**Market**")
        c2.json(BIO_TECHNE_ASSUMPTIONS["market"])
        c3.markdown("**Transaction**")
        c3.json(BIO_TECHNE_ASSUMPTIONS["transaction"])

    if st.button("Run scenarios", type="primary"):
        df = pd.DataFrame(run_scenarios())

        st.subheader("Scenario results")
        st.dataframe(
            df[
                [
                    "scenario",
                    "strategy",
                    "rate_shift_bps",
                    "credit_spread_shift_bps",
                    "dv01",
                    "net_pnl",
                ]
            ].style.format({"dv01": "${:,.0f}", "net_pnl": "${:,.0f}"}),
            width="stretch",
            hide_index=True,
        )

        st.subheader("Net P&L by strategy")
        pivot = df.pivot_table(
            index="scenario", columns="strategy", values="net_pnl", aggfunc="first"
        )
        st.dataframe(pivot.style.format("${:,.0f}"), width="stretch")


# ── 6 · Q&A ───────────────────────────────────────────────────────────────────
elif page.startswith("6"):
    st.title("Document Q&A")
    st.caption("Answers are grounded in the extracted fields only, with citations.")

    document_id = _doc_picker()
    if document_id:
        preset = [
            "What is the consideration per share?",
            "What approval, tender, or acceptance threshold applies?",
            "What is the outside or long-stop date?",
            "How may that date be extended?",
            "What termination fees apply?",
            "What triggers each fee?",
            "How are options, RSUs, PSUs, and other awards treated?",
            "What regulatory approvals are required?",
            "Is there a financing condition?",
            "What financing arrangements are disclosed?",
            "What remedy or burdensome-condition limitations apply?",
            "Which provisions are most relevant to a deal-contingent hedge?",
        ]
        question = st.selectbox("Preset question", preset)
        custom = st.text_input("Or ask your own")
        final_question = custom or question

        if st.button("Ask", type="primary"):
            if not api_key:
                st.error("Enter an Anthropic API key in the sidebar.")
            else:
                rows = [
                    r
                    for r in get_extracted_fields(conn, document_id)
                    if r["status"] == "found" and r["normalized_value"] is not None
                ]
                if not rows:
                    st.warning(
                        "No asserted fields for this document — every extraction was withheld "
                        "or the document has not been extracted yet."
                    )
                else:
                    context = "\n".join(
                        f"{r['field_name']}: {r['normalized_value']} "
                        f"[layer {r['document_layer']}, page "
                        f"{r['printed_page'] or r['pdf_page']}, confidence {r['confidence']:.2f}] "
                        f"evidence: \"{r['evidence']}\""
                        for r in rows
                    )
                    prompt = (
                        "You are a derivatives analyst answering questions about a "
                        "transaction agreement.\n\n"
                        "Answer using ONLY the extracted data below. If the answer is not "
                        "supported by it, say: \"I could not identify sufficient source "
                        "support for this answer.\"\n\n"
                        "For each answer give: the direct answer; the supporting evidence "
                        "quote; the layer and page reference; and whether the statement is "
                        "a fact, an assumption, or analysis.\n\n"
                        f"EXTRACTED DATA:\n{context}\n\n"
                        f"QUESTION: {final_question}"
                    )
                    with st.spinner("Querying…"):
                        response = _anthropic_client(api_key).messages.create(
                            model=MODEL_ID,
                            max_tokens=2048,
                            messages=[{"role": "user", "content": prompt}],
                        )
                    st.markdown("### Answer")
                    st.markdown(response.content[0].text)
                    st.caption(f"{len(rows)} asserted fields used as context · model `{MODEL_ID}`")
