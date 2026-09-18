"""
Shared test fixtures.

Parsing the 99-page development filing takes several seconds, so it is done
once per session and shared. Without this the characterisation tests dominate
the suite's runtime, and a slow suite is a suite that stops being run.
"""

from __future__ import annotations

import os

import pytest

from deallens.ingestion import ingest, load_pdf, segment_layers

BIO_TECHNE_PDF = "8k bio-techne.pdf"
BIO_TECHNE_URL = (
    "https://investors.bio-techne.com/all-sec-filings/content/"
    "0001999371-26-013527/0001999371-26-013527.pdf"
)


def bio_techne_available() -> bool:
    return os.path.exists(BIO_TECHNE_PDF)


requires_bio_techne = pytest.mark.skipif(
    not bio_techne_available(),
    reason=f"{BIO_TECHNE_PDF} not present; fetch it from {BIO_TECHNE_URL}",
)


@pytest.fixture(scope="session")
def bio_techne_bytes() -> bytes:
    if not bio_techne_available():
        pytest.skip(f"{BIO_TECHNE_PDF} not present")
    with open(BIO_TECHNE_PDF, "rb") as handle:
        return handle.read()


@pytest.fixture(scope="session")
def bio_techne_inventory(bio_techne_bytes):
    return load_pdf(bio_techne_bytes, BIO_TECHNE_PDF, source_url=BIO_TECHNE_URL)


@pytest.fixture(scope="session")
def bio_techne_layers(bio_techne_inventory):
    return segment_layers(bio_techne_inventory)


@pytest.fixture(scope="session")
def bio_techne_ingested(bio_techne_bytes):
    return ingest(
        bio_techne_bytes,
        BIO_TECHNE_PDF,
        run_id="test-run",
        source_url=BIO_TECHNE_URL,
    )
