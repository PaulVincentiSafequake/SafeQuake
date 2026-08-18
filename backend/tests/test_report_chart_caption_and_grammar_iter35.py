"""Iteration 35 — extra assertions requested for A1 chart caption + narrative
singular/plural regression, on the LIVE endpoints (operational + public PDFs).
"""
import io
import os
import re
from pathlib import Path

import pytest
import requests


def _load_env():
    fe = Path("/app/frontend/.env").read_text()
    be = Path("/app/backend/.env").read_text()
    base = re.search(r"^EXPO_PUBLIC_BACKEND_URL=(.+)$", fe, re.M).group(1).strip().rstrip("/")
    tok = re.search(r"^ADMIN_TRIGGER_PASSWORD=(.+)$", be, re.M).group(1).strip()
    return base, tok


BASE_URL, ADMIN_TOKEN = _load_env()
API = f"{BASE_URL}/api"
HDR = {"X-Admin-Token": ADMIN_TOKEN}


def _pdf_text(b: bytes) -> str:
    try:
        from pypdf import PdfReader
    except ImportError:
        from PyPDF2 import PdfReader  # type: ignore
    r = PdfReader(io.BytesIO(b))
    return "\n".join((p.extract_text() or "") for p in r.pages)


@pytest.mark.parametrize("path", [
    "/admin/casualty-report/operational.pdf?detail=summary",
    "/admin/casualty-report/operational.pdf?detail=full",
    "/admin/casualty-report/public.pdf",
])
def test_pdfs_render_and_are_pdf(path):
    r = requests.get(API + path, headers=HDR, timeout=60)
    assert r.status_code == 200, r.text[:200]
    assert r.headers["content-type"].startswith("application/pdf")
    assert r.content[:4] == b"%PDF"


@pytest.mark.parametrize("path", [
    "/admin/casualty-report/operational.pdf?detail=summary",
    "/admin/casualty-report/operational.pdf?detail=full",
    "/admin/casualty-report/public.pdf",
])
def test_chart_caption_present(path):
    """A1 invariant: the caption below the chart must appear on ALL variants."""
    r = requests.get(API + path, headers=HDR, timeout=60)
    text = _pdf_text(r.content)
    # PDF text extractors sometimes drop spaces — match a loose form.
    assert re.search(
        r"Each\s*person\s*is\s*counted\s*once,?\s*in\s*the\s*period\s*they\s*first\s*reported\s*that\s*status",
        text,
        re.I,
    ), f"CHART_CAPTION missing in {path}:\n{text[:800]}"


def test_public_pdf_has_no_pii():
    r = requests.get(API + "/admin/casualty-report/public.pdf", headers=HDR, timeout=60)
    text = _pdf_text(r.content)
    # No display names of trapped devices via API
    devs = requests.get(API + "/devices", headers=HDR, timeout=30).json()
    rows = devs.get("devices") if isinstance(devs, dict) else devs
    for r_ in rows or []:
        name = r_.get("display_name")
        if name and len(name) > 3:
            assert name not in text, f"PII leaked in public PDF: {name!r}"
        did = r_.get("device_id")
        if did:
            assert did not in text
        # short_code should not appear either
        sc = r_.get("short_code")
        if sc and len(sc) >= 5:
            assert sc not in text


@pytest.mark.parametrize("path", [
    "/admin/casualty-report/operational.pdf?detail=summary",
    "/admin/casualty-report/operational.pdf?detail=full",
    "/admin/casualty-report/public.pdf",
])
def test_no_grammar_disagreement_in_pdfs(path):
    r = requests.get(API + path, headers=HDR, timeout=60)
    text = _pdf_text(r.content)
    # digit-guarded "1 people" (must not match "31 people")
    m = re.search(r"(?<!\d)\b1 people\b", text)
    assert not m, f"{path}: '1 people' found — {text[max(0, (m.start() if m else 0)-40):(m.end() if m else 0)+40]!r}"
    # "The 1 person" / "The 1 people"
    assert not re.search(r"\bThe 1 (person|people)\b", text, re.I), f"{path}: 'The 1 person/people' found"
    # digit-guarded singular subject + plural verb — "1 person are/have/were"
    m2 = re.search(r"(?<!\d)\b1 person (are|have|were|do)\b", text)
    assert not m2, f"{path}: singular subject with plural verb"
    # plural subject with singular verb (loose)
    # skip strict full-scan for FP-prone cases — the unit tests cover generators exhaustively.
