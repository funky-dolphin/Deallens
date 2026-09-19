"""
app.py
DealLens -- Streamlit front end over the controlled ingestion and extraction
pipeline.

The page order mirrors the order the controls run in. Ingestion and
inspection are local and free; extraction is the one stage that calls the API,
and it stays behind its own button on its own page rather than running as a
side effect of uploading a file.

The API key is read from the environment or Streamlit secrets, never typed
into the page.
"""

from __future__ import annotations

import html
import json
import os
import uuid

import pandas as pd
import streamlit as st
from dotenv import load_dotenv

from deallens.analytics.hedging import (
    ADDITIONAL_ASSUMPTIONS,
    ASSUMPTIONS_VERSION,
    BIO_TECHNE_ASSUMPTIONS,
    RISK_FACTORS,
    RISK_LABELS,
    deal_from_rows,
    probability_weighted,
    risk_exposures,
    run_scenarios,
)
from deallens.comparison import (
    CLASS_ORDER,
    CONFLICT,
    NOT_APPLICABLE,
    classification_counts,
    compare_layers,
)
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
from deallens.extraction import (
    DEFAULT_MODEL_ID,
    MODEL_PROFILES,
    PROMPT_VERSION,
    extract_document,
)
from deallens.ingestion import ingest
from deallens.qa import PRESET_QUESTIONS, answer_question
from deallens.review import apply_correction
from deallens.timeline import build_timeline

load_dotenv()

# Backstop against a pathological document -- one several times larger than
# expected, or whose layers were mis-segmented so the whole filing landed in a
# single layer. extract_document prices the run internally and refuses to start
# above this. It is not shown in the UI; set it to None to remove the gate.
MAX_COST_USD = 25.0

st.set_page_config(page_title="DealLens", layout="wide")

# House palette. Navy carries the interface; red is reserved for figures and
# statuses a reviewer has to act on, so that it still means something when it
# appears.
NAVY = "#1A3668"
RED = "#C8102E"
INK = "#10192B"
MUTED = "#5B6577"
RULE = "#D8DEE8"

st.markdown(
    f"""
    <style>
      /* Tabular figures, so money and dates line up column to column. */
      [data-testid="stDataFrame"] {{ font-variant-numeric: tabular-nums; }}

      /* Masthead rule: a short red lead-in, then navy across the page. This
         is the brand mark, and the one decorative use of red -- everywhere
         else it has to be carrying a meaning. */
      h1 {{
        font-size: 1.45rem; font-weight: 650; color: {INK};
        letter-spacing: -0.01em;
        padding-bottom: 0.45rem; margin-bottom: 0.7rem;
        background-image: linear-gradient(90deg, {RED} 0 3.25rem, {NAVY} 3.25rem);
        background-size: 100% 2px;
        background-position: 0 100%;
        background-repeat: no-repeat;
      }}
      h2 {{
        font-size: 1.05rem; font-weight: 650; color: {NAVY};
        margin-top: 1.4rem; padding-left: 0.55rem;
        border-left: 3px solid {RED};
      }}
      h3 {{ font-size: 0.92rem; font-weight: 650; color: {INK}; }}

      /* Alerts as ruled notices rather than rounded pastel cards. The icon
         is dropped: the words carry the message. */
      [data-testid="stAlert"] {{
        border-radius: 0; border-left: 3px solid {NAVY};
        background: {'#F4F6F9'}; color: {INK}; padding: 0.6rem 0.9rem;
      }}
      [data-testid="stAlert"] svg {{ display: none; }}
      [data-testid="stAlertContentError"],
      [data-testid="stAlertContentWarning"] {{ color: {INK}; }}
      div[data-testid="stAlert"]:has([data-testid="stAlertContentError"]),
      div[data-testid="stAlert"]:has([data-testid="stAlertContentWarning"]) {{
        border-left-color: {RED};
      }}

      section[data-testid="stSidebar"] {{
        border-right: 1px solid {RULE};
        border-top: 3px solid {RED};
      }}
      section[data-testid="stSidebar"] h1 {{
        font-size: 1.05rem; letter-spacing: 0.06em; text-transform: uppercase;
        color: {NAVY};
        background-image: linear-gradient(90deg, {RED} 0 1.6rem, {NAVY} 1.6rem);
        background-size: 100% 2px;
        background-position: 0 100%;
        background-repeat: no-repeat;
        padding-bottom: 0.4rem;
      }}

      /* The one status word a reviewer must not miss. */
      .deallens-flag {{ color: {RED}; font-weight: 650; }}

      .stButton button {{ border-radius: 2px; font-weight: 600; }}
      hr {{ border-color: {RULE}; }}
    </style>
    """,
    unsafe_allow_html=True,
)


# ── Session state ─────────────────────────────────────────────────────────────
# By default the database is in-memory and per browser session, so concurrent
# users on a deployment cannot see one another's documents.
#
# Set DEALLENS_DB to a path to keep it on disk instead. That is a development
# convenience with a real cost, so it is opt-in rather than the default: a
# refresh starts a new Streamlit session and discards session state, which
# discards an in-memory database along with the extraction it holds -- and
# re-running that extraction is the one step in this pipeline that spends
# money. On a file-backed database, everything downstream of extraction stays
# browsable across refreshes. Do not set it on a shared deployment, where one
# file would be one database shared by every visitor.
DB_PATH = os.getenv("DEALLENS_DB") or ":memory:"

# The connection is keyed on the path, not merely created once. Setting
# DEALLENS_DB while the app is already running changes DB_PATH on the next
# rerun but leaves an existing session holding its original connection, so
# writes would keep going to the in-memory database this setting was meant to
# replace -- while the sidebar, reading DB_PATH directly, reported the file.
# Storage that silently disagrees with what the UI claims is worse than no
# setting at all.
#
# The IngestionResult objects are held in session state whichever mode is in
# use: the database records what ingestion found, but extraction needs the
# live object (page text, layer boundaries) and the original bytes. They are
# dropped when the database changes underneath them, because they describe
# documents the new database may know nothing about. Re-ingesting is local
# and free.
if st.session_state.get("db_path") != DB_PATH:
    previous = st.session_state.get("db")
    if previous is not None:
        previous.close()
    st.session_state.db = initialize_schema(get_connection(DB_PATH))
    st.session_state.db_path = DB_PATH
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


def _layer_cell(reading) -> object | None:
    """
    What one layer's value column shows.

    A blank cell would otherwise mean two different things: the layer never
    mentioned the field, or it did and a control withheld what it said. The
    second keeps its page, so a bare blank next to a page number reads as a
    missing value rather than a deliberate one.
    """
    if reading.normalized_value is not None:
        return reading.normalized_value
    if reading.raw_value:
        return "withheld — see review queue"
    return None


def _stat(column, label: str, value: str, alert: bool = False) -> None:
    """
    One summary figure, smaller than `st.metric` renders them.

    `st.metric` sets its value at roughly 2.25rem, which for five figures
    across a row reads as a dashboard headline rather than a document
    summary. `alert` turns the value red, and is for a state a reviewer has
    to act on -- not for emphasis. Values are escaped because this is the one
    place the app emits raw HTML.
    """
    colour = RED if alert else INK
    column.markdown(
        f"<div style='font-size:0.72rem;text-transform:uppercase;letter-spacing:0.04em;"
        f"color:{MUTED}'>{html.escape(label)}</div>"
        f"<div style='font-size:1.05rem;font-weight:600;line-height:1.5;"
        f"color:{colour}'>{html.escape(value)}</div>",
        unsafe_allow_html=True,
    )


def _red_if_negative(value) -> str:
    """
    Red for a loss, and for nothing else.

    The palette keeps red out of headers, rules and chrome so that when it
    does appear in a figure it carries information rather than decoration.
    """
    return f"color: {RED}" if isinstance(value, (int, float)) and value < 0 else ""


def _page_cell(value) -> str:
    """
    A page number as text, never as a figure.

    Pages arrive as a printed label ("A-47", "iii") or a PDF page integer. In
    one pandas column the integers alone become float64, so page 3 renders as
    "3.0000" and a missing page as "NaN". A page is an identifier, not a
    quantity, so it is displayed as the string it is.
    """
    return "" if value is None else str(value)


def _red_if_conflict(value) -> str:
    """Red on the classifications that need a human, and on no others."""
    return (
        f"color: {RED}; font-weight: 600"
        if isinstance(value, str) and value in {"conflict", "unresolved"}
        else ""
    )


def _doc_picker(label: str = "Document") -> str | None:
    """Select one persisted document; returns its document_id."""
    docs = get_documents(conn)
    if not docs:
        st.info("No documents ingested yet. Start on **Ingest & inspect**.")
        return None
    options = {f"{d['filename']}  ·  {d['document_id'][:12]}": d["document_id"] for d in docs}
    return options[st.selectbox(label, list(options.keys()))]


api_key = _api_key()


# ── Sidebar ───────────────────────────────────────────────────────────────────
with st.sidebar:
    st.title("DealLens")
    st.caption("AI-assisted transaction & hedging intelligence")
    st.divider()

    page = st.radio(
        "Navigation",
        [
            "1 · Ingest & inspect",
            "2 · Extract",
            "3 · Summary vs. agreement",
            "4 · Timeline & risk map",
            "5 · Review queue",
            "6 · Hedging analysis",
            "7 · Q&A",
        ],
    )
    st.divider()
    st.caption(f"extraction model `{st.session_state.get('model_id', DEFAULT_MODEL_ID)}`")
    st.caption(f"prompt `{PROMPT_VERSION}`")
    if DB_PATH == ":memory:":
        st.caption(
            "storage `in-memory` — a page refresh discards extracted fields. "
            "Set `DEALLENS_DB` to keep them."
        )
    else:
        st.caption(f"storage `{DB_PATH}` — survives a refresh")


# ── 1 · Ingest & inspect ──────────────────────────────────────────────────────
if page.startswith("1"):
    st.title("Ingest & inspect")
    st.caption(
        "Ingestion reads, classifies and integrity-checks the document locally. "
        "No API request is sent on this page."
    )

    pdf_bytes: bytes | None = None
    filename: str | None = None
    uploaded = st.file_uploader("Choose a PDF", type=["pdf"])
    if uploaded:
        pdf_bytes, filename = uploaded.read(), uploaded.name

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
        # Status reads as words. A green tick and a red cross say less than
        # the status name already does, and a reviewer signing off on an
        # extraction should be reading, not decoding.
        badge = {
            "ingested": "OK",
            "ingested_with_warnings": "CHECK",
            "review_required": "CHECK",
        }.get(status, "BLOCKED")

        # Machine-readability decides whether extraction sends text or page
        # images, which is the largest cost lever in the pipeline, so it
        # belongs in the summary rather than buried in the footnote below.
        machine_readable = bool(doc["is_machine_readable"])
        stats = [
            ("Pages", str(doc["page_count"])),
            ("Machine readable", "Yes" if machine_readable else "No — needs OCR"),
            ("Status", f"{badge} · {status}"),
            ("Structure", doc["transaction_structure"]),
            ("Structure confidence", f"{doc['structure_confidence']:.0%}"),
        ]
        for column, (label, value) in zip(st.columns(len(stats)), stats):
            _stat(column, label, value, alert=label == "Status" and badge != "OK")

        if status == "blocked":
            st.error(
                "Extraction is blocked for this document: pages carry content that could "
                "not be read. Extracting anyway would look complete and silently omit them."
            )

        st.caption(
            f"checksum `{doc['checksum'][:16]}…` · {doc['byte_size']:,} bytes · "
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


# ── 2 · Extract ───────────────────────────────────────────────────────────────
elif page.startswith("2"):
    st.title("Extract")
    st.caption(
        "Sends the document to the Claude API, one request per layer, and stores "
        "the extracted fields with their evidence."
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

        model_ids = list(MODEL_PROFILES)
        model_id = st.selectbox(
            "Extraction model",
            model_ids,
            index=model_ids.index(DEFAULT_MODEL_ID),
            format_func=lambda mid: MODEL_PROFILES[mid].label,
        )
        st.session_state.model_id = model_id

        if not ingestion.may_extract:
            st.error(
                f"Extraction is blocked: ingestion status is "
                f"'{ingestion.integrity.ingestion_status}'. "
                f"{len(ingestion.integrity.unreadable_pages)} page(s) require OCR."
            )

        if st.button("Run extraction", type="primary", disabled=not ingestion.may_extract):
            if not api_key:
                st.error(
                    "No Anthropic API key found. Set `ANTHROPIC_API_KEY` in `.env`, "
                    "or in Streamlit secrets when deployed."
                )
            else:
                with st.spinner("Extracting…"):
                    run = extract_document(
                        _anthropic_client(api_key),
                        ingestion,
                        pdf_bytes,
                        max_cost_usd=MAX_COST_USD,
                        model_id=model_id,
                    )

                if run.error:
                    st.error(run.error)
                else:
                    save_extraction(conn, run)
                    counts = run.by_status()
                    st.success(
                        f"Extracted {len(run.fields)} fields from {len(run.layers)} layer(s) "
                        f"· run `{run.run_id}` · model `{run.model_id}`"
                    )
                    c1, c2, c3 = st.columns(3)
                    c1.metric("Found", counts.get("found", 0))
                    c2.metric("Not found", counts.get("not_found", 0))
                    c3.metric(
                        "Conflict / unresolved",
                        counts.get("conflict", 0) + counts.get("unresolved", 0),
                    )
                    st.caption(
                        f"{run.total_input_tokens:,} input tokens "
                        f"({run.total_cache_read_tokens:,} read from cache) · "
                        f"{run.total_output_tokens:,} output tokens"
                    )

                if run.warnings:
                    with st.expander(f"Warnings ({len(run.warnings)})"):
                        for warning in run.warnings:
                            st.write(f"- {warning}")


# ── 3 · Summary vs. agreement ─────────────────────────────────────────────────
elif page.startswith("3"):
    st.title("Summary vs. agreement")
    st.caption(
        "Each field is extracted from the 8-K filing summary and from the operative "
        "agreement separately, then compared here. Where the two disagree, both "
        "readings are kept and the field is classified rather than resolved."
    )

    document_id = _doc_picker()
    if document_id:
        rows = get_extracted_fields(conn, document_id)
        if not rows:
            st.info("Nothing extracted for this document yet. See **Extract**.")
        else:
            comparisons = compare_layers(rows)
            counts = classification_counts(comparisons)

            columns = st.columns(max(len(counts), 1))
            for column, (name, count) in zip(columns, counts.items()):
                column.metric(name.replace("_", " "), count)

            st.caption(
                "Source hierarchy: on a conflict the operative agreement governs, "
                "because it is the executed contract and the filing summary is a "
                "description of it. `governed by` names the layer that wins; both "
                "values, pages and quotes are kept either way, and the export "
                "carries the governing value with its own page and locator."
            )
            st.caption(
                "`critical` marks fields the assignment forbids inferring silently — "
                "they are held to a higher confidence bar and routed to review on any "
                "conflict. It describes the field, not the result: a critical field "
                "that matches is in good shape."
            )

            default_classes = [
                name for name in CLASS_ORDER if name in counts and name != NOT_APPLICABLE
            ]
            chosen = st.multiselect(
                "Show classifications",
                [name for name in CLASS_ORDER if name in counts],
                default=default_classes,
                format_func=lambda name: name.replace("_", " "),
            )

            visible = [c for c in comparisons if c.classification in chosen]
            if not visible:
                st.info("No fields in the selected classifications.")

            grouped: dict[str, list] = {}
            for comparison in visible:
                grouped.setdefault(comparison.category or "uncategorised", []).append(comparison)

            for category, items in grouped.items():
                with st.expander(f"**{category}** ({len(items)})", expanded=True):
                    st.dataframe(
                        pd.DataFrame(
                            [
                                {
                                    "field": c.field_name,
                                    # A property of the field, not of the
                                    # comparison: critical fields are held to a
                                    # higher confidence bar. It is deliberately
                                    # not a warning icon -- a critical field
                                    # that matches is in good shape.
                                    "critical": c.is_critical,
                                    "classification": c.classification.replace("_", " "),
                                    "filing summary": _layer_cell(c.summary),
                                    "summary p.": _page_cell(c.summary.page),
                                    "agreement": _layer_cell(c.agreement),
                                    "agreement p.": _page_cell(c.agreement.page),
                                    # Two sources are compared, so two value
                                    # columns. The governing value is always a
                                    # copy of one of them; naming the layer that
                                    # governs says the same thing without
                                    # presenting it as a third reading. Only a
                                    # conflict actually needs it -- everywhere
                                    # else there is nothing to choose between.
                                    "governed by": (
                                        c.preferred_layer
                                        if c.classification == CONFLICT
                                        else ""
                                    ),
                                }
                                for c in items
                            ]
                        ).style.map(_red_if_conflict, subset=["classification"]),
                        width="stretch",
                        hide_index=True,
                    )

            # Both readings in full, for the fields where the difference matters.
            needs_attention = [c for c in visible if c.needs_review]
            if needs_attention:
                st.subheader(f"Conflicts and unresolved fields ({len(needs_attention)})")
                for comparison in needs_attention:
                    label = " · critical field" if comparison.is_critical else ""
                    with st.expander(
                        f"`{comparison.field_name}` — "
                        f"{comparison.classification.replace('_', ' ')}{label}"
                    ):
                        st.write(comparison.reason)
                        for title, reading in (
                            ("Filing summary", comparison.summary),
                            ("Operative agreement", comparison.agreement),
                        ):
                            st.markdown(f"**{title}**")
                            if reading.raw_value or reading.evidence:
                                st.markdown(
                                    f"- value: `{reading.normalized_value}` "
                                    f"(raw: {reading.raw_value!r})\n"
                                    f"- page {reading.page} · {reading.section or 'no section'}\n"
                                    f"- status: `{reading.status}` · "
                                    f"confidence {reading.confidence:.0%}"
                                )
                                if reading.evidence:
                                    st.caption(f"“{reading.evidence}”")
                            else:
                                st.caption("Nothing was read from this layer.")

            with st.expander("Extracted field rows, per layer (audit record)"):
                st.caption(
                    f"{len(rows)} rows — one per field per layer, which is what the "
                    "comparison above is computed from."
                )
                st.dataframe(
                    pd.DataFrame(
                        [
                            {
                                "field": r["field_name"],
                                "layer": r["document_layer"],
                                "value": r["normalized_value"],
                                "ccy": r["currency"],
                                "status": r["status"],
                                "conf": r["confidence"],
                                "page": _page_cell(r["printed_page"] or r["pdf_page"]),
                                "evidence ok": r["evidence_verified"],
                                "review": r["review_status"],
                                "run": r["run_id"],
                                "evidence": r["evidence"],
                            }
                            for r in rows
                        ]
                    ),
                    width="stretch",
                    hide_index=True,
                )

            st.download_button(
                "Download comparison (JSON)",
                data=json.dumps([c.to_dict() for c in comparisons], indent=2, default=str),
                file_name=f"{document_id}_summary_vs_agreement.json",
                mime="application/json",
            )


# ── 4 · Timeline & risk map ───────────────────────────────────────────────────
elif page.startswith("4"):
    st.title("Timeline & risk map")
    st.caption(
        "Dates the agreement fixes, dates it derives, and dates it only "
        "describes. Entries with no calendar position are kept out of the "
        "ordered timeline rather than being given one."
    )

    document_id = _doc_picker()
    if document_id:
        rows = get_extracted_fields(conn, document_id)
        if not rows:
            st.info("Nothing extracted for this document yet. See **Extract**.")
        else:
            timeline = build_timeline(rows)
            horizon = timeline.horizon

            st.subheader("Hedge horizon")
            if horizon.is_complete:
                c1, c2, c3, c4 = st.columns(4)
                c1.metric("Signing", horizon.signing_date)
                c2.metric("Outside date", horizon.outside_date)
                c3.metric(
                    "Window",
                    f"{horizon.base_days} d",
                    delta=(
                        f"+{horizon.extension_days} d extendable"
                        if horizon.extension_days
                        else None
                    ),
                    delta_color="off",
                )
                c4.metric("Conditions outstanding", horizon.outstanding_conditions)
                if horizon.final_outside_date:
                    st.caption(
                        f"Worst case close **{horizon.final_outside_date}** — "
                        f"{horizon.total_days} days from signing."
                    )
            else:
                st.warning(
                    "No hedge horizon could be built: the timeline has no fixed "
                    "signing and outside date pair."
                )
            for note in horizon.notes:
                st.caption(f"· {note}")

            st.subheader(f"Dated timeline ({len(timeline.anchored)})")
            if timeline.anchored:
                st.dataframe(
                    pd.DataFrame(
                        [
                            {
                                "date": e.resolved_date,
                                "event": e.event,
                                "kind": e.kind.replace("_", " "),
                                "basis": e.basis,
                                "derivation": e.derivation or "",
                                "stated as": e.stated_as,
                                "layer": e.layer,
                                "p.": _page_cell(e.page),
                            }
                            for e in timeline.anchored
                        ]
                    ),
                    width="stretch",
                    hide_index=True,
                )
                if timeline.calculated:
                    st.caption(
                        f"{len(timeline.calculated)} date(s) marked `calculated` are "
                        "derived from the agreement's extension clause by arithmetic "
                        "— the document does not state them in those words. The "
                        "derivation column shows the working."
                    )
            else:
                st.info("No entry resolved to a calendar date.")

            st.subheader(f"Conditional, relative and estimated ({len(timeline.unanchored)})")
            st.caption(
                "These have no fixed position: each depends on an event, a "
                "condition, a party's election, or is a non-binding estimate."
            )
            if timeline.unanchored:
                st.dataframe(
                    pd.DataFrame(
                        [
                            {
                                "event": e.event,
                                "section": e.section,
                                "kind": e.kind.replace("_", " "),
                                "stated as": e.stated_as,
                                "trigger": e.trigger or "",
                                "layer": e.layer,
                                "p.": _page_cell(e.page),
                                "review": e.review_status,
                            }
                            for e in timeline.unanchored
                        ]
                    ),
                    width="stretch",
                    hide_index=True,
                )

            with st.expander("Evidence for every timeline entry"):
                for entry in timeline.anchored + timeline.unanchored:
                    st.markdown(
                        f"**{entry.event}** — `{entry.kind}` · `{entry.basis}`"
                        + (f" · derived: {entry.derivation}" if entry.derivation else "")
                    )
                    if entry.evidence:
                        st.caption(f"“{entry.evidence}”")
                    st.caption(
                        f"{entry.layer or 'no layer'} · page {entry.page} · "
                        f"{entry.section_ref or 'no section'}"
                    )

            st.download_button(
                "Download timeline (JSON)",
                data=json.dumps(timeline.to_dict(), indent=2, default=str),
                file_name=f"{document_id}_timeline.json",
                mime="application/json",
            )


# ── 5 · Review queue ──────────────────────────────────────────────────────────
elif page.startswith("5"):
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
            mark = "CRITICAL" if row["is_critical"] else "standard"
            with st.expander(f"{mark} **{row['field_name']}** — {row['status']}"):
                if row["is_critical"]:
                    st.markdown(
                        "<span class='deallens-flag'>CRITICAL FIELD</span> — "
                        "held to a higher confidence bar; a wrong value here is "
                        "materially worse than an honest absence.",
                        unsafe_allow_html=True,
                    )
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

                st.divider()
                st.markdown("**Correct this field**")
                st.caption(
                    "Supplying a value here normalizes it with the same code "
                    "used for a model reading, records the supersede in the "
                    "audit notes, and releases the field to Q&A, the timeline "
                    "and the hedging horizon. Leave blank to annotate only."
                )

                with st.form(key=f"correct-{row['id']}"):
                    new_value = st.text_input(
                        "Value, as the document writes it",
                        value=row["raw_value"] or "",
                        placeholder="e.g. $250,000,000",
                    )
                    f1, f2 = st.columns([3, 1])
                    new_evidence = f1.text_input(
                        "Evidence quote", value=row["evidence"] or ""
                    )
                    new_page = f2.number_input(
                        "PDF page",
                        min_value=0,
                        value=int(row["pdf_page"] or 0),
                        step=1,
                    )
                    note = st.text_input("Reviewer note")

                    b1, b2, b3 = st.columns(3)
                    apply_clicked = b1.form_submit_button(
                        "Apply correction", type="primary"
                    )
                    verified_clicked = b2.form_submit_button("Mark verified")
                    exception_clicked = b3.form_submit_button("Keep as exception")

                if apply_clicked:
                    outcome = apply_correction(
                        conn,
                        row["id"],
                        new_value,
                        evidence=new_evidence or None,
                        pdf_page=int(new_page) or None,
                        reviewer_note=note or None,
                    )
                    if outcome.accepted:
                        st.success(
                            f"`{outcome.field_name}` set to "
                            f"`{outcome.normalized_value}` "
                            f"(method `{outcome.extraction_method}`)."
                        )
                        if outcome.evidence_verified is False:
                            st.warning(
                                "The quote was not found on the cited page. The "
                                "correction stands on your authority and the "
                                "mismatch is recorded in the notes."
                            )
                        st.rerun()
                    else:
                        st.error(outcome.reason)
                elif verified_clicked:
                    set_review_status(conn, row["id"], "verified", note or None)
                    st.rerun()
                elif exception_clicked:
                    set_review_status(conn, row["id"], "exception", note or None)
                    st.rerun()


# ── 6 · Hedging analysis ──────────────────────────────────────────────────────
elif page.startswith("6"):
    st.title("Hedging & financing analysis")
    st.caption("All market and financing inputs below are **synthetic assumptions**.")

    # Timing is not an assumption -- it comes from the agreement, via the
    # Workstream 4 timeline. Selecting a document here is what lets the delay
    # scenarios be dated instead of stubbed.
    horizon = None
    document_id = _doc_picker("Date the delay scenarios from")
    if document_id:
        rows = get_extracted_fields(conn, document_id)
        if rows:
            horizon = build_timeline(rows).horizon
    if horizon is not None and horizon.extension_dates:
        st.caption(
            f"Delay scenarios dated from the timeline: outside date "
            f"`{horizon.outside_date}`, extensions to "
            f"{', '.join(f'`{d}`' for d in horizon.extension_dates)} "
            "(calculated from the extension clause)."
        )
    else:
        st.caption(
            "No extension dates available for this document, so no closing "
            "delay is assumed. The delay scenarios report as unavailable "
            "rather than being given an invented length."
        )

    with st.expander("Assumptions", expanded=False):
        st.caption(
            f"Assumptions version `{ASSUMPTIONS_VERSION}`. The block below is the "
            "assignment's standardized input, verbatim and synthetic — none of it "
            "is extracted from the agreement."
        )
        c1, c2, c3 = st.columns(3)
        c1.markdown("**Financing**")
        c1.json(BIO_TECHNE_ASSUMPTIONS["financing"])
        c2.markdown("**Market**")
        c2.json(BIO_TECHNE_ASSUMPTIONS["market"])
        c3.markdown("**Transaction**")
        c3.json(BIO_TECHNE_ASSUMPTIONS["transaction"])

        st.markdown("**Additional synthetic assumptions**")
        st.caption(
            "Inputs the assignment does not supply but the required scenarios "
            "cannot be priced without. Each is stated with the reason it is needed."
        )
        st.dataframe(
            pd.DataFrame(
                [
                    {"assumption": name, "value": value, "why it is required": reason}
                    for name, (value, reason) in ADDITIONAL_ASSUMPTIONS.items()
                ]
            ),
            width="stretch",
            hide_index=True,
        )

    st.caption(
        "**Sign convention:** positive is a gain to the issuer, negative a cost. "
        "The coupon is not yet fixed, so a rise in rates is a loss. Every figure "
        "is the change against the base case."
    )

    if st.button("Run scenarios", type="primary"):
        deal = deal_from_rows(rows) if document_id and rows else None
        results = run_scenarios(horizon=horizon, deal=deal)
        frame = pd.DataFrame([r.to_dict() for r in results])

        st.subheader("Scenario results")
        st.caption(
            "One table per scenario; one row per strategy. Columns are the seven "
            "risks the analysis keeps separate — a zero means the strategy is not "
            "exposed to that risk in that scenario, not that it was ignored."
        )

        money = {RISK_LABELS[f]: "${:,.0f}" for f in RISK_FACTORS}
        money["Net P&L"] = "${:,.0f}"

        for scenario_id in frame["scenario_id"].unique():
            rows_for = [r for r in results if r.scenario_id == scenario_id]
            heading = rows_for[0].scenario
            if rows_for[0].delay_days is not None:
                heading += f" · {rows_for[0].delay_days} days → {rows_for[0].delayed_to}"
            st.markdown(f"**{heading}**")

            table = pd.DataFrame(
                [
                    {
                        "Strategy": r.strategy,
                        **{RISK_LABELS[f]: r.attribution.get(f, 0.0) for f in RISK_FACTORS},
                        "Net P&L": r.net_pnl,
                    }
                    for r in rows_for
                ]
            )
            st.dataframe(
                table.style.format(money).map(_red_if_negative),
                width="stretch",
                hide_index=True,
            )
            for note in dict.fromkeys(n for r in rows_for for n in r.notes):
                st.caption(f"· {note}")
            st.divider()

        st.subheader("Risk exposure by strategy")
        st.caption(
            "What each strategy is exposed to per basis point, including risks "
            "no required scenario happens to shock. A risk a strategy carries is "
            "still a risk it carries."
        )
        st.dataframe(
            pd.DataFrame(risk_exposures(deal=deal)).rename(
                columns={f: RISK_LABELS[f] for f in RISK_FACTORS}
            ),
            width="stretch",
            hide_index=True,
        )

        st.subheader("Probability-weighted outcome")
        st.caption(
            "Weighted over the delay and failure scenarios only. The rate shocks "
            "are sensitivities, not outcomes with a likelihood."
        )
        weighted = probability_weighted(results)
        for column, (strategy, value) in zip(st.columns(len(weighted)), weighted.items()):
            _stat(column, strategy, f"${value:,.0f}")

        st.download_button(
            "Download scenarios (JSON)",
            data=json.dumps(
                {
                    "assumptions_version": ASSUMPTIONS_VERSION,
                    "assumptions": BIO_TECHNE_ASSUMPTIONS,
                    "additional_assumptions": {
                        name: {"value": value, "why_required": reason}
                        for name, (value, reason) in ADDITIONAL_ASSUMPTIONS.items()
                    },
                    "results": [r.to_dict() for r in results],
                },
                indent=2,
                default=str,
            ),
            file_name="hedging_scenarios.json",
            mime="application/json",
        )


# ── 7 · Q&A ───────────────────────────────────────────────────────────────────
elif page.startswith("7"):
    st.title("Document Q&A")
    st.caption("Answers are grounded in the extracted fields only, with citations.")

    document_id = _doc_picker()
    if document_id:
        question = st.selectbox("Preset question", PRESET_QUESTIONS)
        custom = st.text_input(
            "Or ask your own",
            placeholder="e.g. What happens to PSUs granted before the agreement date?",
        )
        final_question = custom.strip() or question
        if custom.strip():
            st.caption(f"Asking your question: “{custom.strip()}”")

        if st.button("Ask", type="primary"):
            if not api_key:
                st.error(
                    "No Anthropic API key found. Set `ANTHROPIC_API_KEY` in `.env`, "
                    "or in Streamlit secrets when deployed."
                )
            else:
                rows = get_extracted_fields(conn, document_id)
                with st.spinner("Querying…"):
                    # Q&A always runs on the default model. The extraction
                    # toggle scopes to extraction on purpose: answering costs
                    # a fraction of a run, and holding the answer model fixed
                    # keeps a cheaper extraction's effect visible in the
                    # answers rather than confounded with a cheaper answerer.
                    answer = answer_question(
                        _anthropic_client(api_key), final_question, rows
                    )

                st.markdown("### Answer")
                if answer.text:
                    st.markdown(answer.text)
                else:
                    st.error("No answer was returned.")
                for warning in answer.warnings:
                    st.warning(warning)
                st.caption(
                    f"{answer.fields_used} asserted field(s) used as context · "
                    f"model `{answer.model_id}` · prompt `{PROMPT_VERSION}`"
                )
