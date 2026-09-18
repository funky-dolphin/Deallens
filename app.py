"""
app.py
DealLens — AI-Assisted Transaction and Hedging Intelligence
Streamlit application entry point.
"""

import streamlit as st
import pandas as pd
import os
from dotenv import load_dotenv

from database import get_connection, initialize_schema, insert_document, insert_extracted_fields, get_all_fields, get_documents
from extractor import extract_from_pdf
from hedging import run_scenarios, BIO_TECHNE_ASSUMPTIONS

load_dotenv()

# ── Page config ──────────────────────────────────────────────────────────────
st.set_page_config(
    page_title="DealLens",
    page_icon="🔍",
    layout="wide"
)

# ── Session state ─────────────────────────────────────────────────────────────
if "db" not in st.session_state:
    conn = get_connection()
    initialize_schema(conn)
    st.session_state.db = conn

if "processed_docs" not in st.session_state:
    st.session_state.processed_docs = []

conn = st.session_state.db

# ── Sidebar ───────────────────────────────────────────────────────────────────
with st.sidebar:
    st.title("🔍 DealLens")
    st.caption("AI-Assisted Transaction & Hedging Intelligence")
    st.divider()

    api_key = st.text_input(
        "Anthropic API Key",
        type="password",
        value=os.getenv("ANTHROPIC_API_KEY", ""),
        help="Your Anthropic API key. Never stored or logged."
    )

    st.divider()
    page = st.radio("Navigation", [
        "📄 Upload & Extract",
        "📊 Transaction Data",
        "📈 Hedging Analysis",
        "🔎 Q&A"
    ])

# ── Upload & Extract ──────────────────────────────────────────────────────────
if page == "📄 Upload & Extract":
    st.title("Upload Transaction Document")
    st.caption("Upload a merger agreement, 8-K filing, or takeover offer PDF.")

    uploaded_file = st.file_uploader(
        "Choose a PDF file",
        type=["pdf"],
        help="Accepts SEC filings, merger agreements, and takeover documents."
    )

    source_url = st.text_input(
        "Source URL (optional)",
        placeholder="https://...",
        help="Public URL where this document was obtained."
    )

    if uploaded_file and st.button("🚀 Process Document", type="primary"):
        if not api_key:
            st.error("Please enter your Anthropic API key in the sidebar.")
        else:
            pdf_bytes = uploaded_file.read()
            status_box = st.empty()
            progress_bar = st.progress(0)

            def update_progress(chunk_num, total_chunks, message):
                status_box.caption(f"⏳ {message}")
                if total_chunks > 0:
                    progress_bar.progress(min(chunk_num / total_chunks, 1.0))

            result = extract_from_pdf(
                pdf_bytes=pdf_bytes,
                filename=uploaded_file.name,
                source_url=source_url or None,
                api_key=api_key,
                progress_callback=update_progress
            )
            progress_bar.empty()
            status_box.empty()

            if result["error"]:
                st.error(f"Extraction failed: {result['error']}")
            else:
                # Store in database
                insert_document(conn, result["document_meta"])
                insert_extracted_fields(
                    conn,
                    result["document_meta"]["document_id"],
                    result["fields"],
                    result["run_id"]
                )
                st.session_state.processed_docs.append(result["document_meta"]["document_id"])
                pages = result.get("page_count", "?")
                chunks = result.get("chunk_count", "?")
                st.success(f"✅ Extracted {len(result['fields'])} fields from {uploaded_file.name} ({pages} pages, {chunks} chunks)")
                st.caption(f"Document ID: `{result['document_meta']['document_id']}` | Run ID: `{result['run_id']}`")

                # Preview extracted fields
                st.subheader("Extracted Fields Preview")
                fields_df = pd.DataFrame(result["fields"])
                if not fields_df.empty:
                    display_cols = ["field_name", "normalized_value", "currency", "confidence", "evidence"]
                    display_cols = [c for c in display_cols if c in fields_df.columns]
                    st.dataframe(fields_df[display_cols], use_container_width=True)

# ── Transaction Data ──────────────────────────────────────────────────────────
elif page == "📊 Transaction Data":
    st.title("Transaction Data")

    docs = get_documents(conn)
    if not docs:
        st.info("No documents processed yet. Go to Upload & Extract to get started.")
    else:
        doc_options = {d["filename"]: d["document_id"] for d in docs}
        selected_name = st.selectbox("Select Document", list(doc_options.keys()))
        selected_id = doc_options[selected_name]

        fields = get_all_fields(conn, selected_id)
        if fields:
            df = pd.DataFrame(fields)

            # Group by category
            categories = {
                "Transaction Identity": ["target_company", "acquirer_company", "merger_subsidiary", "agreement_date", "transaction_type", "consideration_per_share", "consideration_currency", "total_transaction_value"],
                "Timing": ["expected_closing_date", "outside_date", "long_stop_date", "extension_conditions"],
                "Conditions": ["shareholder_approval_threshold", "antitrust_approvals_required", "financing_condition", "material_adverse_effect_condition"],
                "Termination": ["target_termination_fee", "parent_termination_fee", "fee_triggers"],
                "Financing": ["funding_sources", "bridge_financing_amount", "bridge_financing_currency", "debt_commitment"]
            }

            for category, field_names in categories.items():
                cat_df = df[df["field_name"].isin(field_names)][["field_name", "normalized_value", "currency", "confidence", "evidence", "section", "page"]]
                if not cat_df.empty:
                    with st.expander(f"**{category}**", expanded=True):
                        st.dataframe(cat_df, use_container_width=True)
        else:
            st.warning("No fields extracted for this document.")

# ── Hedging Analysis ──────────────────────────────────────────────────────────
elif page == "📈 Hedging Analysis":
    st.title("Hedging & Financing Analysis")
    st.caption("All market and financing inputs below are **synthetic assumptions** unless otherwise noted.")

    with st.expander("📋 Assumptions", expanded=False):
        col1, col2, col3 = st.columns(3)
        with col1:
            st.markdown("**Financing**")
            st.json(BIO_TECHNE_ASSUMPTIONS["financing"])
        with col2:
            st.markdown("**Market**")
            st.json(BIO_TECHNE_ASSUMPTIONS["market"])
        with col3:
            st.markdown("**Transaction**")
            st.json(BIO_TECHNE_ASSUMPTIONS["transaction"])

    if st.button("▶️ Run Scenarios"):
        with st.spinner("Running hedging scenarios..."):
            scenarios = run_scenarios()

        df = pd.DataFrame(scenarios)

        st.subheader("Scenario Results")
        st.dataframe(
            df[["scenario", "strategy", "rate_shift_bps", "credit_spread_shift_bps", "dv01", "net_pnl"]].style.format({
                "dv01": "${:,.0f}",
                "net_pnl": "${:,.0f}",
            }),
            use_container_width=True
        )

        # Pivot for comparison
        st.subheader("Net P&L by Strategy")
        pivot = df.pivot_table(
            index="scenario",
            columns="strategy",
            values="net_pnl",
            aggfunc="first"
        )
        st.dataframe(pivot.style.format("${:,.0f}"), use_container_width=True)

# ── Q&A ───────────────────────────────────────────────────────────────────────
elif page == "🔎 Q&A":
    st.title("Document Q&A")

    docs = get_documents(conn)
    if not docs:
        st.info("No documents processed yet. Go to Upload & Extract to get started.")
    else:
        doc_options = {d["filename"]: d["document_id"] for d in docs}
        selected_name = st.selectbox("Select Document", list(doc_options.keys()))
        selected_id = doc_options[selected_name]

        # Preset questions from the assignment
        preset_questions = [
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
            "Which provisions are most relevant to a deal-contingent hedge?"
        ]

        question = st.selectbox("Choose a question", preset_questions)
        custom = st.text_input("Or ask your own question")
        final_question = custom if custom else question

        if st.button("Ask") and api_key:
            fields = get_all_fields(conn, selected_id)
            context = "\n".join([
                f"{f['field_name']}: {f['normalized_value']} (evidence: {f['evidence']}, page: {f['page']})"
                for f in fields if f['normalized_value'] and f['normalized_value'] != 'null'
            ])

            with st.spinner("Querying..."):
                import anthropic
                client = anthropic.Anthropic(api_key=api_key)
                response = client.messages.create(
                    model="claude-opus-4-5",
                    max_tokens=1024,
                    messages=[{
                        "role": "user",
                        "content": f"""You are a derivatives analyst answering questions about a transaction agreement.

Answer based ONLY on the extracted data below. If the answer is not supported by the data, say:
"I could not identify sufficient source support for this answer."

For each answer include:
- Direct answer
- Supporting evidence (quote from document)
- Page/section reference
- Whether this is a fact, assumption, or analysis

EXTRACTED DATA:
{context}

QUESTION: {final_question}"""
                    }]
                )

            st.markdown("### Answer")
            st.markdown(response.content[0].text)
