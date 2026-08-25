"""Audit-log exports and the dual casualty reports (B1 / B2).

Extracted verbatim from server.py on 2026-06-18 — behaviour-unchanged. The
file had grown past 6,000 lines, which made every export change a search
problem before it was a code problem. Nothing here is new; the module
boundary is the change.

Contents:
  * dashboard settings (org logo, authority name) read helpers
  * confidentiality banner + watermark + logo drawing for PDFs
  * response-over-time chart and the plain-language progress narrative
  * GET /api/admin/audit-log/export.csv  and  .pdf
  * GET /api/admin/casualty-report/operational.pdf   (B1)
  * GET /api/admin/casualty-report/public.pdf        (B2)

Privacy locks live with the code that enforces them — see the B2 endpoint
and /app/memory/PRD.md "Legal / privacy locks".
"""
from __future__ import annotations

import html as _html
import re as _re
from datetime import datetime, timedelta, timezone
from typing import Optional

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import Response

from auth import require_role, resolve_principal
from deps import ADMIN_TRIGGER_PASSWORD, db, short_code as _short_code

router = APIRouter()
# Endpoints below were declared on server.py's `api_router` (prefix /api) and
# are included into it unchanged, so every path stays byte-identical.
api_router = router


# ---------- Audit log exports (CSV + PDF) ---------------------------------
#
# Both endpoints are admin-gated (same auth as /api/admin/audit-log HTML
# view). They return the same data as GET /api/audit, but formatted for
# offline archival, incident reporting, and Malta Civil Protection
# handover packages.
#
# Design decisions:
#
#  1. **CSV includes ALL fields flat.** Every event kind gets its own
#     column footprint — trigger events populate magnitude/recipients,
#     status events populate lat/lon/battery, rescued events populate
#     rescued_by/notes/prior_status. Empty cells where a field doesn't
#     apply. This means the CSV is wide but any single row is
#     unambiguously self-describing — no cross-referencing needed when
#     the file lands on someone's desk two months later.
#
#  2. **PDF is landscape A4, table-first, no logo/branding chrome.**
#     Reports get printed and archived; a colourful header wastes ink and
#     confuses non-Quake-Angel readers ("what is this branding, am I
#     looking at a marketing document?"). We're producing evidence.
#
#  3. **Since/until filters can span arbitrary windows.** The CSV path
#     accepts up to 30 days at a time (capped server-side); PDF caps at
#     500 events per document (readability). Longer windows → paginate
#     the request client-side.
#
#  4. **Notes are included in BOTH exports** (admin-gated, so no
#     leak-to-public risk). Redaction happens at storage time via
#     /api/admin/redact-notes if operationally needed.
#
#  5. **UTC timestamps in ISO 8601, always.** Malta uses CET/CEST which
#     shifts twice a year — any local-time timestamp on a paper report
#     becomes ambiguous the moment DST changes. Reader can convert if
#     they need local.

import csv as _csv
import io as _io

MAX_EXPORT_WINDOW_DAYS = 30
MAX_EXPORT_ROWS = 500


# ─── Dashboard settings (org logo, next-of-kin authority name, etc.) ─────
#
# Single-document collection `dashboard_settings` with well-known _id="global".
# Motivation:
#   - Some things (org logo bytes, authority name for casualty reports) are
#     deployment-specific and must NOT be hard-coded. "Malta Civil Protection"
#     was hard-coded in the B2 report footer, which implied an operational
#     agreement that didn't exist and would have been misleading if the
#     PDF ever reached press. Also blocks selling the platform to other
#     civil-protection agencies without a source-code fork.
#   - This is not per-country — it's per-deployment. If Quake Angel later
#     multi-tenants, this becomes per-tenant. For now, single doc.
#
# Fields:
#   authority_name    : str  — what to render in the B2 report footer. When
#                              unset, the report uses the generic phrasing
#                              "the responsible authorities" (Paul, 2026-08-10).
#   logo_b64          : str  — base64-encoded PNG or SVG. Bounded at 200 KB
#                              PNG / 100 KB SVG at the API layer.
#   logo_mime         : "image/png" | "image/svg+xml"
#   logo_updated_at   : datetime
#   logo_updated_by   : str
DASHBOARD_SETTINGS_ID = "global"

async def _get_dashboard_settings() -> dict:
    """Fetch the single dashboard-settings doc. Never raises — returns {}
    if the collection is empty, so callers can `settings.get(...)` freely
    without null-guarding a missing doc."""
    doc = await db.dashboard_settings.find_one({"_id": DASHBOARD_SETTINGS_ID})
    return doc or {}


async def _get_authority_name() -> str:
    """Resolve the "responsible authorities" phrase used in casualty
    reports and the B1/B2 legal footers. Falls back to a generic wording
    when no specific authority has been configured — this is intentional:
    naming an authority we have no agreement with (e.g. "Malta Civil
    Protection") in a distributable PDF creates a legal + reputational
    exposure that a generic phrase does not.

    #297 (2026-08-24 — Paul, live test): "Authority: Emergency test name"
    was printed on a real downloadable public report. It was not an
    unfilled template — somebody typed that during a test and every
    export faithfully repeated it for weeks. A placeholder-looking value
    now falls back to the neutral wording instead of being published, and
    the settings endpoint refuses to accept one in the first place. Two
    layers on purpose: the refusal protects new mistakes, the fallback
    protects the value already saved in a live database.
    """
    settings = await _get_dashboard_settings()
    name = (settings.get("authority_name") or "").strip()
    if not name or looks_like_a_placeholder(name):
        return "the responsible authorities"
    return name


# Words that mean "somebody was trying this out". A distributable report is
# read by families, journalists and possibly a court; none of them can tell
# a placeholder from a real agency name, so we refuse to print one.
_PLACEHOLDER_WORDS = (
    "test", "testing", "example", "sample", "demo", "dummy", "placeholder",
    "tbd", "tba", "todo", "xxx", "asdf", "foo", "bar", "lorem",
)
_PLACEHOLDER_PHRASES = ("your name", "name here", "enter ")


def looks_like_a_placeholder(value: str) -> bool:
    """True when a typed authority name reads as a test entry.

    Matched on WORD boundaries, not substrings: "Attest Rescue" contains
    the letters of "test" and is somebody's real name. Refusing a real
    agency would be its own kind of wrong.

    Deliberately conservative on the other side too: a name is refused if
    it is a single character or has no letters or digits in it, because
    that is nobody's agency either.
    """
    v = (value or "").strip().lower()
    if len(v) < 2:
        return True
    if not any(ch.isalnum() for ch in v):
        return True
    if any(p in v for p in _PLACEHOLDER_PHRASES):
        return True
    words = set(_re.findall(r"[a-z0-9]+", v))
    return bool(words & set(_PLACEHOLDER_WORDS))


async def _get_authority_cooperation_claim() -> bool:
    """Whether the public report may state that Quake Angel is operating
    'in cooperation with' the named authority. Default False (D2 lock,
    Batch 7 2026-08-19). The neutral wording — "Authority: [name]" — is
    always safe; the cooperation phrasing implies an operational partnership
    and next-of-kin notification which must not be asserted until an admin
    has knowingly enabled it. See PRD standing rule #86."""
    settings = await _get_dashboard_settings()
    return bool(settings.get("authority_cooperation_claim") is True)


# ─── Confidentiality banner (PDF + CSV) ─────────────────────────────────
#
# Every export that contains personal data (CSV audit, PDF audit, B1
# operational report) must carry a visible confidentiality notice on
# EVERY page (PDF) or as the FIRST ROW (CSV). B2 Public is exempt — it
# contains only aggregate counts and is designed for press/family use.
#
# Wording locked in by Paul on 2026-08-10. Do not paraphrase or shorten
# the text below without checking with him — the current phrasing was
# specifically negotiated to (a) invoke GDPR by name, (b) tell the reader
# where to get a shareable version instead of just refusing, (c) not
# assume any specific enforcement regime beyond GDPR (which the EU-wide
# deployment target actually gives us jurisdiction over).
CONFIDENTIALITY_TEXT = (
    "CONFIDENTIAL — contains personal data including precise locations "
    "and device identifiers. Do not share outside authorised emergency "
    "personnel. Handling is subject to GDPR. For public, press or family "
    "communication use the public \u201Csafe to share\u201D report instead."
)


def _pdf_confidentiality_onpage(canvas_, doc_):
    """Reportlab onPage callback — draws a red confidentiality banner
    across the top of every page and a matching footer line at the
    bottom. Used by CSV/audit PDF and B1 operational PDF; explicitly
    NOT used by B2 Public.

    Colour choice: dark red (#B0141A) — WCAG-AA on white background for
    the 10pt banner text. On B&W printers this shows up as ~50% grey,
    still legible; combined with the ALL-CAPS "CONFIDENTIAL" prefix and
    bold face, the label carries even when colour is stripped.
    """
    from reportlab.lib import colors as _rl_colors
    canvas_.saveState()
    w, h = doc_.pagesize   # width, height in points

    # Margin-band watermark on EVERY page — rotated "CONFIDENTIAL"
    # running up BOTH side margins. Replaced the centred diagonal
    # watermark (issue #131): the diagonal crossed the Rescued table
    # row and the chart legend on some layouts. The 12 mm side margins
    # are guaranteed content-free on every confidential document, so
    # the mark can be bolder AND can never obscure report data.
    canvas_.setFont("Helvetica-Bold", 11)
    try:
        canvas_.setFillAlpha(0.30)
    except Exception:
        pass  # very old reportlab without alpha — banner still carries
    canvas_.setFillColor(_rl_colors.HexColor("#B0141A"))
    _wm = "CONFIDENTIAL"
    _wm_w = canvas_.stringWidth(_wm, "Helvetica-Bold", 11)
    _y = 60.0
    while _y + _wm_w < h - 80:   # stop short of the top banner + header logos
        for _x in (20.0, w - 11.0):   # left + right margin bands
            canvas_.saveState()
            canvas_.translate(_x, _y)
            canvas_.rotate(90)
            canvas_.drawString(0, 0, _wm)
            canvas_.restoreState()
        _y += _wm_w + 60
    try:
        canvas_.setFillAlpha(1)
    except Exception:
        pass

    # Top band: 30pt tall with a 13pt bold CONFIDENTIAL — the single most
    # important line on the document must be legible at a glance, even on
    # a mis-scaled print (bug 1b, 2026-08-13; was 8pt in a 22pt band).
    banner_h = 30
    canvas_.setFillColor(_rl_colors.HexColor("#B0141A"))
    canvas_.rect(0, h - banner_h, w, banner_h, fill=1, stroke=0)
    canvas_.setFillColor(_rl_colors.white)
    canvas_.setFont("Helvetica-Bold", 13)
    canvas_.drawString(12, h - 14, "CONFIDENTIAL — personal data")
    canvas_.setFont("Helvetica", 7.5)
    # Fits a single line on portrait A4 at 7.5pt (verified).
    canvas_.drawString(12, h - 25,
        "Do not share outside authorised emergency personnel. "
        "For public use the \u201Csafe to share\u201D public report."
    )
    # Footer band
    canvas_.setFillColor(_rl_colors.HexColor("#B0141A"))
    canvas_.setFont("Helvetica-Bold", 7)
    canvas_.drawString(12, 8, "CONFIDENTIAL · GDPR-protected · use the \u201Csafe to share\u201D public report for external distribution")
    # Page number on the right — trivial but reviewers ask for it
    canvas_.setFont("Helvetica", 7)
    canvas_.setFillColor(_rl_colors.HexColor("#666666"))
    canvas_.drawRightString(w - 12, 8, f"Page {doc_.page}")
    canvas_.restoreState()





def _round5(v):
    """GDPR data-minimisation: coordinates leave the system at 5 decimal
    places (~1 m). Device-reported accuracy is 5–19 m, so further digits
    carry no information — only privacy risk."""
    try:
        return round(float(v), 5)
    except (TypeError, ValueError):
        return v


async def _backfill_display_names(events: list[dict]) -> None:
    """Fill missing `display_name` on export rows from the device's
    CURRENT record. Historical status_events predate display_name being
    snapshotted onto every event, so exports showed a permanently blank
    column — which reads as data loss."""
    missing = {e["device_id"] for e in events
               if e.get("device_id") and not e.get("display_name")}
    if not missing:
        return
    rows = await db.device_status.find(
        {"device_id": {"$in": list(missing)}},
        {"_id": 0, "device_id": 1, "display_name": 1},
    ).to_list(len(missing))
    names = {r["device_id"]: r["display_name"] for r in rows if r.get("display_name")}
    for e in events:
        if not e.get("display_name") and e.get("device_id") in names:
            e["display_name"] = names[e["device_id"]]


# ─── Operator pseudonymisation on export ────────────────────────────────
# Optional (query param `pseudonymise=true`): operator emails in
# triggered_by / rescued_by / reverted_by become stable aliases like
# "operator-3". The real mapping lives server-side in
# `operator_pseudonyms` so accountability is preserved — an admin can
# always resolve who operator-3 is, but a leaked export can't.

async def _operator_alias(identity: str) -> str:
    row = await db.operator_pseudonyms.find_one({"identity": identity}, {"_id": 0, "alias": 1})
    if row:
        return row["alias"]
    n = await db.operator_pseudonyms.count_documents({})
    alias = f"operator-{n + 1}"
    try:
        await db.operator_pseudonyms.insert_one({
            "identity": identity,
            "alias": alias,
            "created_at": datetime.now(timezone.utc).isoformat(),
        })
    except Exception:
        row = await db.operator_pseudonyms.find_one({"identity": identity}, {"_id": 0, "alias": 1})
        if row:
            return row["alias"]
    return alias


async def _pseudonymise_events(events: list[dict]) -> None:
    """In-place: replace personal operator identities (anything with an
    '@') with stable server-side aliases. Non-personal attributions like
    'dashboard' pass through unchanged."""
    cache: dict = {}
    for e in events:
        for f in ("triggered_by", "rescued_by", "reverted_by"):
            v = e.get(f)
            if isinstance(v, str) and "@" in v:
                if v not in cache:
                    cache[v] = await _operator_alias(v)
                e[f] = cache[v]


# ─── Credential detection for free-text notes ───────────────────────────
# An admin password leaked into a rescue note previously, forcing an
# urgent rotation + audit redaction. Server-side gate: reject anything
# resembling a credential BEFORE it is stored. Deliberately errs on the
# side of caution — a false rejection costs a reworded note; a false
# accept costs another credential rotation.
_CREDENTIAL_KEYWORD_RE = _re.compile(
    r"(?i)\b(password|passwd|pwd|passphrase|api[_ -]?key|secret|token|bearer|credentials?)\b"
    r"\s*(?:is|was|[:=])?\s*(\S{6,})"
)
_CREDENTIAL_TOKEN_RES = [
    _re.compile(r"eyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{5,}"),                 # JWT
    _re.compile(r"\bAKIA[0-9A-Z]{16}\b"),                                     # AWS access key
    _re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----"),                       # PEM key
    _re.compile(r"\b(?:sk|pk|rk|ghp|gho|xox[abps])[_-][A-Za-z0-9_-]{16,}\b"), # common API-key prefixes
]


def _looks_like_credential(text: str) -> Optional[str]:
    """Returns a human-readable reason if `text` appears to contain a
    credential, else None."""
    for rx in _CREDENTIAL_TOKEN_RES:
        if rx.search(text):
            return "contains what looks like an API key, token or private key"
    m = _CREDENTIAL_KEYWORD_RE.search(text)
    if m:
        tail = m.group(2)
        # Only fire when the token after the keyword looks secret-shaped
        # (digits/symbols or mixed case) — "asked for the password" must pass.
        if _re.search(r"[0-9!@#$%^&*_\-+=/\\]", tail) or (
            tail != tail.lower() and tail != tail.upper() and len(tail) >= 8
        ):
            return f"looks like a credential following the word '{m.group(1)}'"
    for tok in _re.findall(r"\S{24,}", text):
        if (_re.search(r"[a-z]", tok) and _re.search(r"[A-Z]", tok)
                and _re.search(r"[0-9]", tok)):
            return "contains a long random-looking string"
    return None


# ─── Org logo on PDF headers ─────────────────────────────────────────────

def _logo_is_brand_duplicate(logo_bytes: bytes) -> bool:
    """True when an uploaded org logo is visually the Quake Angel mark
    itself (issue A2): re-exports of our own artwork used to print the
    mark TWICE on report headers. Mirrors the dashboard's check
    (looksLikeBrandMark in index.html): composite both onto the dark
    header colour at 16×16, compare RGB, mean-abs-diff < 12
    (measured: same artwork ≈ 1.4, a real partner logo ≈ 166)."""
    try:
        import base64
        from PIL import Image

        def _sig(data: bytes) -> bytes:
            im = Image.open(_io.BytesIO(data)).convert("RGBA")
            bg = Image.new("RGBA", (16, 16), (15, 15, 15, 255))
            bg.alpha_composite(im.resize((16, 16)))
            return bg.convert("RGB").tobytes()

        a = _sig(logo_bytes)
        b = _sig(base64.b64decode(_QA_LOGO_B64))
        diff = sum(abs(x - y) for x, y in zip(a, b)) / len(a)
        return diff < 12
    except Exception:
        return False   # if in doubt keep the logo — never hide a real partner


async def _get_logo_image_reader():
    """PNG org logo as a reportlab ImageReader, or None. SVG cannot be
    rasterised by reportlab, so an SVG logo appears on the dashboard but
    not on PDFs (documented limitation). A logo that visually duplicates
    the Quake Angel mark is treated as absent (A2) — same rule the
    dashboard header applies."""
    s = await _get_dashboard_settings()
    b64, mime = s.get("logo_b64"), s.get("logo_mime")
    if not b64 or mime != "image/png":
        return None
    import base64
    from reportlab.lib.utils import ImageReader
    try:
        raw = base64.b64decode(b64)
        if _logo_is_brand_duplicate(raw):
            return None
        return ImageReader(_io.BytesIO(raw))
    except Exception:
        return None


# Quake Angel's own mark, embedded so every report carries the product
# branding permanently (issue #128): before this, an uploaded partner
# logo was the ONLY mark on B1/B2 headers, inverting the hierarchy.
_QA_LOGO_B64 = "iVBORw0KGgoAAAANSUhEUgAAAFAAAABQCAYAAACOEfKtAAALWElEQVR4nO2ce7BdVX3HP7+19z777PO4JwnhEQnmASGTgDwKCVokWCUKoxgGyuhQHekf1qlOW2hheEynY9vR6Qz0D5RHpCKDMsZWxYggQnEUMxqLApZCE0AEwiNCQsjj3vPYr59/rHNiQu4595yz981jcr4z586de89e+7e++7d+r/XbS2q1mjLC0DAHWoBDHSMCM2JEYEa4vf5pZH+JcXAj7eEluhKoCvVwOsQ59FDwuivTpASqgu/BornTKdahAQFe3gL11uQk7kOgEat5i+bCz77kYIDDNc5RBb8AK69KWfekUi1Bmu79nd42sP05bAlkai/bk0Dd43M4op+5j8KYjBgRmBEjAjNiRGBGjAjMiBGBGTEiMCNGBGbEiMCMGBGYEdNCoB6Eud90yZQ7gYKtYKgeHER2ZPAL0zN+bgQqVtgwhhc3C56rOObAkqgKrgNGlBc3C0nau7o8DHIj0AgEY8rWHcp5V5b417s8jKOYA0SiKjgOJKpcs7rAR64JqDdTSlVFctyqyEygKngubHodPvdvBb77iIPnCjesKXP9Vzxc17K3PzlUBRFQlCu/VOC275fwXOHrD7pccWOBLdutZubxYLMTiNU+Y5Tv/cznC9+o0ggNR9SU1feWuPUeh6Cs+1RypxOqUAyUG77pcvdDJY6aqby1y+Gf76xy//oCnqPkpYSZCRQsib/aYCgWoOwrSWInUQ2EL95d4sGfC+UyJPuBxCSFUln5r4cNN327RK1iH16aQrUExsCvNprcjFfmYVIF4yqPP+vyxnYHpz2iqtXKJHG46tYSz76kBP6+ewp5Ik2hVIQnNsL1t5dwHGMfsNqH7Bjl1a0uTz7v4Hiai0PJTKARiGM4Y3FMuZiS7CFUmkLRVza94XH9V4q0orZTyXrTSdBxGjsnlGtWB7y506Xg7k1SksJYKeXUE2LSWHJxJpkITFIISvCTxwz/cEsZz5V9DHOSQK2s/Pevfe56wKFYmh57mCr4ReW2tS7rn/YZK+mkJkNE+Jubyjy2wWprVlkyESgCSQTzjlGqgRInkz/VVKFYEG6+p8gLL9ugNs94LFUIivDUc/AfPyhSCSa3tyIQxcKMivKO2Uock1kLc/PC5WJ3m2L3V5VNr3vccb+H62m+61hBRFl9b4Et2108R7uGKKpQKSpIPiJkIjBNwQ2UB35Z4P9e8Cj53QVPUygHsObhIk//Fop+PlqYphAE8D9PCWvX+VS7LF2w5JWKyqMbC/z0iQJekN2RZF7CGguLjouZUUmJk+7fVQXPUbZsd1jzYw/jdid7cDmUux/y2Dnxxyhg0u8BcQKzaykL5yRoJJnjwewEJnBkLaUcKEna27Olag33d37i89yL2LAmA4kd7Xt8I9z3C59KMIVTEEgSoVZOmTWW2u8eSBuYpmB8Zf3TBV7d4uB7vbVKFQqusnmby43/WUDJlhEYgTBSbljjs2PCwXW0p13r2OLnX3N57BkPp4fJ6VuGTBcLpKFw5uKIY2cntKKpvVqSQrWkrF1X5JHHhaCLx5wKSQrFMvxwveHBR4tUgu62rwMRaEXCgjkxp5wQkbSyx4LZCDQQteC0RSlL5sW0wv4EsuGE4abv+NSb7bLXAPftlKl27FBuucdHRPpaiiLQDOG0E2KWzFPCMHsTaeYl7Pvw1O+EJ593KfqK9qFN1iMrj/yvz7ceHjy47gTNd9zn8uiGgg2h+rheFYIC/Hqjy3OvCMUc4tHMTiSKYe6RypwjUqK4P00AQMFzhNvv9Xlzmy2J9WOP0nbP3sub4c4HfAJf+iZBsAXfuUelHDNLiZLMPiQ7gWEExxwNH1we0hpgSaQKga9seKnA7fe6FIpT2zBoL19Pufkej02ve1M6rrfLG8Xw4feEzJhpfz/gmYgxEDbh2ZedgW1ZqlAuwm1rA9b/RqhU7aS6IYqhXIUH1wt3/Sjoy3G8HSLwzCaHNAf7B3lUxdSSWKv0DiEmvVTBcZRGy+GKmwOeeUGpjlkPu6dWqbbreTXlif9Xrr61RJqaoQgQYEZFkZy2GnIp6buucPn5IeViMnB1o1Pyev6VAh//fJmH1kO5/McNqY7HLQXK935s+It/qbD5TQ+/MFga1slCapWEy1ZGCAPY6x7Ipy6rdjlE8XASpSkEReWVLR6f+kKVq77sUW/Z2qHrwPZx5bP/XuAzN1Z4c6f19sOUoRSr3a5DbsWM7CV9A60QTj5e+dDyFs1QhlpaaQpFT3Ec4cvfLfPFb3gUXAVR/vGrBe64v4RfEFskHYI86/CEj7wn5IS5SvNgsYECRAlUx4RV743aW2LDjZW2L501BmvX+fx+G/zuVeGHv/SZXbPedti4zZb0Uy4+NyQoSW77M7ksYQFIlZ0TQhhnMy2qYFAmmsL2cWHrDmi2U66sRj+MhfF6ToXANnIh0BhoNYVL359w0TlN6q3hljFgKyYqVEtKrazMGrNZS5oOH7MZA42WcNl5DS7405Rmg55lr4HGzmMQwcZo1RqcsThhvG6FHnasOIZjZqXMHIOjZipH1lLiZHi9FqDeVM5amhCUIc5xTya31g7HQFgXVp0Ts3JZa2gtFGMT/uVLIgIfZlTg9BOjru+qTQUjVvsuOqfFymUJrQnBOZhaOzropHUL5sPZ74oYbwyuhUasvVu2JOTKj0WEkaAqXPuJkJPmR7SiwctPxsBEE849LeIdx+aTvu01fn5DtbVwXLh4RcyyxSGNAbVQBHZMwMUrWsyZYx9IM4SF823+un18MNtlBOpNYcWpLc4/K6E1LrnZvt33yHMwaW+yn7gQ3rUwtsuuzzt0anV/dnqLVe9NCet2so6BqCF87P0xZy21cWa/GmSMfU31zMUx8+dB3EfBd1Dk32ApEDaEv7s0Yun8iEYfVd9OkPvOo2PuvK7OcUeze89WBKIQFs+Dr13b4IixuOv+856wS1dYviTkrz4a0RqXoR1bz/vkPWCnZHTiQmXlmSGNEDynd2wo2BRrZkWplYVWuLemiEAY2gB7rGybl6aSwTHQipQPvzvkncfZPDhv7YNp6pF2DDTGhWs/GXHJigavbDF9RdfGsRHupBMVELUNQr2I6DQTvbpF+MsLGnz2z2Pqu/K3fR1MW5e+AuWi8E+Xh3xm1QS+1z3879iqU4+PKZXb2rLH/0Xs32bU4KQFCRPN7s5EgUqQcsWlE1xzWYjvTu/JGdNGoA1J4Lij4ObrWrzv9CZv7TIUvL3J6cRpJ80PufyCmKRH15Sq8OkLQxbMifZxJiL2nIdtOw0Xnt3khqtDZs+QXDaOes5z+oZu79vGEI0bPn1hxKK5IW+8ZdqJPbsr2NVSwu1X11m6EFpdAmYj0GzCspPhlivreG6y29Y5xlZzfr/NcMrxLT75wZhwp5k2u7eXXNM7fDsMieDdpyi3/n2dj55dp1RM2T4uTDSF17Yazj+rxZ8sgYkpUkBjoD4OK05Xzj0t5LWthomm8NYuYUYl4ZL31Vl9VYNTF9uQZbrs3p7oeWZCXrATF5YvhTVnNLn7/piv3Rdw5MyUY2fH/PVFEVHUZ9Ddbs+47hMhs2vKa1tdtu0y/O0ldVZ9ICGcMNQnhs/FB4W8/fQ2I9agn7wA1uV87Emn1bZY6NxLwYE4lIFSLFV7GI7jKZoAKig2EBfyW7adY0/OG/bYk7zRaSDoTFSxHa1GBpt0J+9O31b9PhBHVe1XAjvoTFR2/xgcIuRaVRkWo7c1M2JEYEaMCMyIEYEZMSIwI0YEZkTv09v0MD+9rY+37rsSKGKj8MP6/EAFp9A7yN8nlYP26wg+LJ0vu19nPSzRbt3b8JKyq0uhY1ICwZLYaE23hIcGggJdm0e7LmEj9gXlEUDT7quwpxPZn6/pH6oYhTEZMSIwI0YEZsQfAN1poZrz4VedAAAAAElFTkSuQmCC"
_QA_LOGO_READER = None


def _get_qa_logo_reader():
    global _QA_LOGO_READER
    if _QA_LOGO_READER is None:
        import base64
        from reportlab.lib.utils import ImageReader
        _QA_LOGO_READER = ImageReader(_io.BytesIO(base64.b64decode(_QA_LOGO_B64)))
    return _QA_LOGO_READER


def _draw_header_logos(canvas_, doc_, partner_logo, top_offset_pt: float):
    """Top-right header marks: the permanent Quake Angel logo rightmost,
    with the optional partner logo to its LEFT under an explicit
    'In partnership with' caption. Hierarchy (issue #128): Quake Angel
    is ALWAYS present; a partner mark is always labelled, never a
    replacement."""
    from reportlab.lib import colors as _rl_colors
    try:
        page_w, page_h = doc_.pagesize
        lh = 26.0
        x_right = page_w - 14
        qa = _get_qa_logo_reader()
        qw, qh = qa.getSize()
        qa_w = qw * lh / float(qh or 1)
        canvas_.drawImage(
            qa, x_right - qa_w, page_h - top_offset_pt - lh,
            width=qa_w, height=lh, mask="auto", preserveAspectRatio=True,
        )
        if partner_logo is not None:
            iw, ih = partner_logo.getSize()
            lw = iw * lh / float(ih or 1)
            px_right = x_right - qa_w - 10
            canvas_.saveState()
            canvas_.setFont("Helvetica-Oblique", 5.5)
            canvas_.setFillColor(_rl_colors.HexColor("#777777"))
            canvas_.drawRightString(px_right, page_h - top_offset_pt + 2, "In partnership with")
            canvas_.restoreState()
            canvas_.drawImage(
                partner_logo, px_right - lw, page_h - top_offset_pt - lh,
                width=lw, height=lh, mask="auto", preserveAspectRatio=True,
            )
    except Exception:
        pass  # a broken logo must never block an emergency report


def _make_confidential_onpage(logo=None):
    """Banner + margin watermark + footer on every page, plus the
    permanent Quake Angel mark (and the partner logo when configured)."""
    def _onpage(canvas_, doc_):
        _pdf_confidentiality_onpage(canvas_, doc_)
        _draw_header_logos(canvas_, doc_, logo, top_offset_pt=42)
    return _onpage


def _make_public_onpage(logo=None):
    """B2 Public: Quake Angel mark + optional labelled partner logo
    top-right, no confidentiality chrome."""
    def _onpage(canvas_, doc_):
        _draw_header_logos(canvas_, doc_, logo, top_offset_pt=16)
    return _onpage


# ─── Response-over-time chart (B1 + B2) ─────────────────────────────────

def _bucket_timeline(raw_rows: list[dict], since_dt: datetime, until_dt: datetime):
    """Bucket status_events into hourly (window <= 48h) or daily buckets.

    COUNTS PEOPLE ONCE, NOT EVENTS (bug 2026-08-18 #124, reopened as A1
    2026-06-18).
    ------------------------------------------------------------------
    First it added 1 per status_event, so one person toggling their status
    three times produced a red bar of 3 while the narrative said "1 person
    told us they were trapped". That was fixed by de-duplicating WITHIN a
    bucket — but a person who is still trapped an hour later reports again,
    and the C1 re-check ladder makes that the normal case, so the same
    person produced a red bar of 1 in each of three consecutive hours. A
    reader adds the bars up and reads three trapped people while the
    sentence underneath says one. Individually correct figures, document
    still contradicts itself in plain language — and on the public report
    that misreading reaches the press and cannot be corrected afterwards.

    So each device is now counted ONCE PER STATUS FOR THE WHOLE WINDOW, in
    the bucket where it FIRST reported that status. The invariant that the
    document depends on:

        sum of the red bars == "N people told us they were trapped"

    is therefore true by construction, and the chart reads as "when people
    first told us", which is what a response-over-time chart is for.
    """
    hourly = (until_dt - since_dt).total_seconds() <= 48 * 3600
    step = timedelta(hours=1) if hourly else timedelta(days=1)
    start = since_dt.replace(minute=0, second=0, microsecond=0)
    if not hourly:
        start = start.replace(hour=0)
    buckets: list[dict] = []
    index: dict = {}
    t = start
    while t <= until_dt:
        import timefmt as _tf
        _tl = _tf.local(t) or t
        label = _tl.strftime("%d %b %H:%M") if hourly else _tl.strftime("%d %b")
        index[t] = len(buckets)
        buckets.append({"t": t, "label": label, "trapped": 0, "safe": 0, "rescued": 0})
        t += step

    # (device_id, status) -> earliest bucket index in which it appears.
    # One person contributes at most one unit to each of the three series,
    # however many times they report.
    KNOWN = ("trapped", "rescued", "safe")
    first_bucket: dict[tuple[str, str], int] = {}

    for row in raw_rows:
        ra = row.get("recorded_at")
        try:
            dt = datetime.fromisoformat(str(ra).replace("Z", "+00:00"))
        except (ValueError, TypeError):
            continue
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        key = dt.replace(minute=0, second=0, microsecond=0)
        if not hourly:
            key = key.replace(hour=0)
        i = index.get(key)
        if i is None:
            continue
        st = row.get("status")
        if st == "rescued" and row.get("rescue_reverted"):
            continue
        if st not in KNOWN:
            continue
        did = row.get("device_id")
        if not did:
            # The narrative counts distinct device_ids; a row without one
            # cannot be attributed to a person, so it must not become a bar.
            continue
        k = (did, st)
        if k not in first_bucket or i < first_bucket[k]:
            first_bucket[k] = i

    for (_did, st), i in first_bucket.items():
        buckets[i][st] += 1

    return buckets, hourly


def _timeline_chart(buckets: list[dict], width_pt: float, height_pt: float):
    """Grouped bar chart Drawing: trapped / rescued / safe per bucket,
    with a manual legend (plain words, no jargon)."""
    from reportlab.graphics.shapes import Drawing, Rect, String
    from reportlab.graphics.charts.barcharts import VerticalBarChart
    from reportlab.lib import colors as C

    d = Drawing(width_pt, height_pt)
    # Opaque white base so the page watermark never bleeds through the
    # plot area or legend (Paul, 2026-08-13 polish item).
    d.add(Rect(0, 0, width_pt, height_pt, fillColor=C.white, strokeColor=None))
    chart = VerticalBarChart()
    chart.x, chart.y = 34, 30
    chart.width, chart.height = width_pt - 44, height_pt - 52
    s_trap = [b["trapped"] for b in buckets]
    s_resc = [b["rescued"] for b in buckets]
    s_safe = [b["safe"] for b in buckets]
    chart.data = [s_trap, s_resc, s_safe]
    chart.bars[0].fillColor = C.HexColor("#C21818")
    chart.bars[1].fillColor = C.HexColor("#1F8A3A")
    chart.bars[2].fillColor = C.HexColor("#7A8CA0")
    chart.bars.strokeColor = None
    chart.groupSpacing = 5
    chart.barSpacing = 1
    max_v = max([1] + s_trap + s_resc + s_safe)
    chart.valueAxis.valueMin = 0
    chart.valueAxis.valueMax = max_v + 1
    chart.valueAxis.valueStep = max(1, (max_v + 1) // 5)
    chart.valueAxis.labels.fontSize = 6
    labels = [b["label"] for b in buckets]
    if len(labels) > 16:
        keep = max(1, len(labels) // 12)
        labels = [l if i % keep == 0 else "" for i, l in enumerate(labels)]
    chart.categoryAxis.categoryNames = labels
    chart.categoryAxis.labels.fontSize = 6
    chart.categoryAxis.labels.angle = 45
    chart.categoryAxis.labels.boxAnchor = "ne"
    d.add(chart)
    # Y-axis label in plain words — the axis had no label at all, so a reader
    # had to guess whether the bars counted people or reports (2026-08-18).
    y_label = String(9, height_pt / 2 - 22, "People")
    y_label.fontSize = 7
    y_label.fillColor = C.HexColor("#444444")
    y_label.textAnchor = "start"
    d.add(y_label)
    # Legend with an opaque white backing strip — the diagonal watermark
    # crosses the top of the drawing and was obscuring "Checked in safe"
    # on B1 (2026-08-13). The strip keeps legend text legible in colour
    # AND in black-and-white print.
    legend_items = (
        ("First told us they were trapped", "#C21818"),
        ("First marked found / rescued", "#1F8A3A"),
        ("First checked in safe", "#7A8CA0"),
    )
    legend_w = sum(10 + len(label) * 3.6 + 18 for label, _ in legend_items)
    d.add(Rect(30, height_pt - 14, legend_w, 13, fillColor=C.white, strokeColor=None))
    lx = 34.0
    for label, color in legend_items:
        d.add(Rect(lx, height_pt - 11, 7, 7, fillColor=C.HexColor(color), strokeColor=None))
        s = String(lx + 10, height_pt - 10, label)
        s.fontSize = 7
        d.add(s)
        lx += 10 + len(label) * 3.6 + 18
    return d


def _plural(n: int, singular: str, plural: str) -> str:
    return singular if n == 1 else plural


def _n_people(n: int) -> str:
    """'1 person' / '4 people'. Every count of humans in a narrative
    sentence goes through here — the singular/plural bug has been found
    three times (#124, A1, egress), always because a count was formatted
    inline at the call site."""
    return "1 person" if n == 1 else f"{n} people"


def _subject_of_still_trapped(n: int, t: int) -> tuple[str, str]:
    """Subject phrase + agreeing verb for 'n of the t people still trapped'.

    Returns e.g. ("The only person still trapped", "has"),
    ("All 3 people still trapped", "have"), ("1 of the 3 people still
    trapped", "has"). Shared by every 'of the still-trapped' sentence so
    the agreement is written once, not once per feature.
    """
    if t == 1:
        return "The only person still trapped", "has"
    if n == t:
        return f"All {t} people still trapped", "have"
    return f"{n} of the {t} people still trapped", _plural(n, "has", "have")


# Stated under every chart, on B1 and B2 alike. Without it a reader has no
# way to know whether adding the bars up is meaningful (A1, 2026-06-18).
CHART_CAPTION = ("Each person is counted once, in the period they first reported that status. "
                 "Adding the red bars together gives the number of people who told us they "
                 "were trapped.")


def _progress_figures(raw_rows: list[dict], latest_events: list[dict], counts: dict) -> dict:
    """All figures for the plain-language narrative.

    CONSISTENCY RULE (bug 2026-08-13): any narrative figure that shares a
    concept with the aggregate table MUST be computed from the SAME source
    as the table. The table once said "People rescued: 0" while the
    narrative said "1 of 1 found" — because a self-reported safe check-in
    was silently merged into "found". Two definitions of one word, a
    paragraph apart, on the report families read.

    - `rescued`       == the table's "People rescued" (operator-confirmed).
    - `still_trapped` == the table's trapped count.
    - `self_safe`     is a SEPARATE figure (self-reported, never merged).
    """
    trapped_ids = {r["device_id"] for r in raw_rows
                   if r.get("status") == "trapped" and r.get("device_id")}
    self_safe = sum(1 for e in latest_events
                    if e.get("device_id") in trapped_ids and e.get("status") == "safe")
    resolved = sum(1 for e in latest_events
                   if e.get("device_id") in trapped_ids and e.get("status") in ("rescued", "safe"))
    return {
        "total_trapped_reports": len(trapped_ids),
        "rescued": int(counts.get("rescued") or 0),
        "still_trapped": int(counts.get("trapped") or 0),
        "self_safe": self_safe,
        "resolved": resolved,
    }


def _window_narrative(raw_rows: list[dict], latest_events: list[dict],
                      counts: dict) -> list[str]:
    """PAST TENSE. Reads WINDOW data only.

    #216 (Batch 7): the narrative under "What happened during this
    window" must speak strictly about the past — what took place during
    the period covered by the report. It must NOT claim current state.
    A sentence saying "1 person is still recorded as trapped" under a
    "during this window" heading is the exact pattern that produced the
    reports/tables contradiction bug — it uses present-tense verbs while
    the reader has been told the section is about a past period.

    HARD RULE (test-enforced): no sentence returned from here may use the
    present-tense state words "is", "are", "still", "yet", "currently",
    "right now", or "cannot". Anything that needs those words is a
    current-state fact and belongs in `_current_state_narrative` instead.

    HARD LOCKS (Paul, 2026-08-12/13, still in force):
    - "found by a rescue team" and "told us themselves they were safe"
      remain two separately-worded figures. Never merged.
    - No percentages anywhere.
    - Singular / plural handled through `_n_people` and `_plural`.
    """
    # `trapped_ids` = every device that told us "trapped" AT ANY POINT
    # in the window (from raw_rows, the un-collapsed event list). That's
    # the past-tense population — someone who reported trapped and later
    # reported safe is still counted here, because during this period
    # they DID tell us they were trapped.
    trapped_ids = {r["device_id"] for r in raw_rows
                   if r.get("status") == "trapped" and r.get("device_id")}
    total = len(trapped_ids)
    if total == 0:
        return [
            "No one told us they were trapped during this period.",
            "This only counts people using the app.",
            "Others may have been affected who we cannot see.",
        ]
    lines = [f"{_n_people(total)} told us they were trapped during this period."]

    # Rescued during the window — from _bucket_by_status(events), which
    # counts one row per device whose LATEST event in the window was
    # "rescued". Past tense: "was / were found by a rescue team".
    resc = int(counts.get("rescued") or 0)
    if resc == 0:
        lines.append("No one was found by a rescue team during this period.")
    else:
        lines.append(
            f"{_n_people(resc)} {_plural(resc, 'was', 'were')} "
            "found by a rescue team during this period."
        )

    # People who reported trapped in this window AND later self-reported
    # safe in this window (latest-event = safe). Past tense throughout.
    ss = sum(1 for e in latest_events
             if e.get("device_id") in trapped_ids and e.get("status") == "safe")
    if ss:
        lines.append(
            f"{_n_people(ss)} who reported being trapped later told us "
            "themselves they were safe."
        )

    lines.append("This only counts people using the app.")
    lines.append("Others may have been affected who we cannot see.")
    return lines


def _current_state_narrative(current_counts, latest_events: list[dict]) -> list[str]:
    """PRESENT TENSE. Reads current state from compute_counts + latest_events.

    #216 (Batch 7): every sentence here describes the situation right
    now. Placed under the "Where things stand right now" heading, below
    the aggregate table. Uses `compute_counts` — the same single source
    of truth the table above uses — so the narrative and the table can
    never disagree.

    HARD RULE (test-enforced): sentences from here MUST NOT use past-tense
    words like "was", "were", or "had been" as tense-carrying verbs. If
    a fact is about what happened during a past period, it belongs in
    `_window_narrative` instead.
    """
    lines: list[str] = []
    needs = int(getattr(current_counts, "needs_help", 0) or 0)
    if needs == 0:
        lines.append("No one is waiting for help right now.")
    else:
        lines.append(
            f"{_n_people(needs)} {_plural(needs, 'is', 'are')} "
            "waiting for help right now."
        )

    not_resp = int(getattr(current_counts, "not_responding", 0) or 0)
    if not_resp > 0:
        lines.append(
            f"We have not heard from {_n_people(not_resp)} in a while."
        )

    # Extraction and low-battery notes are CURRENT-state facts about
    # people who ARE trapped now (`latest_events` filtered to
    # status == "trapped"), so they belong here — not under
    # "What happened during this window".
    lines.extend(_extraction_lines(latest_events))
    lines.extend(_low_battery_lines(latest_events))
    return lines


# Back-compat alias so old call sites (and any tests still referencing
# the old name) do not immediately break. New code MUST call one of the
# two functions above explicitly.
def _plain_language_progress(raw_rows: list[dict], latest_events: list[dict],
                             counts: dict) -> list[str]:
    return _window_narrative(raw_rows, latest_events, counts)


def _fmt_dt_plain(dt: datetime) -> str:
    """'13 Aug 2026, 15:02 (Malta time, UTC+02:00)'.

    #272 (2026-08-21 — Paul): every time a person reads is Malta time, and
    on a legal record the offset is printed beside it, because an inquiry
    may read this in another country. The instant itself is always stored
    in UTC — see timefmt.py for how daylight saving is handled."""
    import timefmt
    return timefmt.legal(dt)


def _fmt_when(ts) -> str:
    """'21 Aug 2026, 21:08' in Malta time, for table cells where the
    heading already names the clock (#272). '—' when we have nothing."""
    import timefmt
    return timefmt.human(ts) if ts else "Not known"


def _duration_words(td: timedelta) -> str:
    """'3 hours and 12 minutes' / '1 day and 4 hours' — whole words, never
    '3h 12m', never '0 hours'."""
    total_min = int(td.total_seconds() // 60)
    if total_min < 1:
        return "less than a minute"
    days, rem = divmod(total_min, 1440)
    hours, minutes = divmod(rem, 60)
    parts = []
    if days:
        parts.append(f"{days} {_plural(days, 'day', 'days')}")
    if hours:
        parts.append(f"{hours} {_plural(hours, 'hour', 'hours')}")
    if minutes and not days:   # over a day reads "1 day and 4 hours"
        parts.append(f"{minutes} {_plural(minutes, 'minute', 'minutes')}")
    if not parts:
        parts.append(f"{minutes} {_plural(minutes, 'minute', 'minutes')}")
    return parts[0] if len(parts) == 1 else " and ".join(parts)


def _covers_line(since_dt: datetime, until_dt: datetime) -> str:
    """Every export states the period it covers in absolute terms — 'Last
    7 days' is meaningless when the document is read next month or in an
    inquiry next year (1c, 2026-08-13)."""
    return (f"Covers {_fmt_dt_plain(since_dt)} to {_fmt_dt_plain(until_dt)} — "
            f"{_duration_words(until_dt - since_dt)}.")


async def _last_alert_start() -> Optional[datetime]:
    """Start of the most recent alert = latest push_events row. There is
    no explicit end-of-incident marker in the data model, so 'active
    alert' is defined by the CALLER (dashboard uses: within 72h)."""
    rows = await db.push_events.find({}, {"_id": 0, "created_at": 1}).sort("created_at", -1).to_list(1)
    if not rows:
        return None
    try:
        dt = datetime.fromisoformat(str(rows[0].get("created_at")).replace("Z", "+00:00"))
        return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
    except (ValueError, TypeError):
        return None


async def _window_gap_warning(since_dt: datetime) -> Optional[str]:
    """1d (2026-08-13): if the export window starts AFTER the alert began,
    the document silently misses the first — probably worst — hours of the
    incident. Say so on the document itself, in plain words."""
    alert = await _last_alert_start()
    if alert is not None and since_dt > alert:
        return ("Warning: this window starts after the alert. It leaves out the first "
                f"{_duration_words(since_dt - alert)} of the incident.")
    return None


def _short_codes_for(device_ids) -> dict:
    """Collision-safe short codes (item 2, 2026-08-13). Rule: last 5
    alphanumeric characters of the device id, uppercased; if two ACTIVE
    devices collide, every collider is extended leftward 2 characters at
    a time until unique. Same map must be used everywhere a code is shown
    so the string matches across card / pin / report / CSV."""
    from collections import Counter
    alnum = {d: _re.sub(r"[^A-Za-z0-9]", "", str(d)) for d in device_ids if d}
    # Contract (iteration 25): ids too short to be meaningful get NO code.
    alnum = {d: a for d, a in alnum.items() if len(a) >= 3}
    codes: dict = {}
    remaining = list(alnum)
    length = 5
    while remaining:
        trial = {d: alnum[d][-min(length, len(alnum[d])):].upper() for d in remaining}
        counts = Counter(trial.values())
        nxt = []
        for d, code in trial.items():
            if counts[code] == 1 or len(code) >= len(alnum[d]):
                codes[d] = code
            else:
                nxt.append(d)
        remaining = nxt
        length += 2
    return codes


async def _trapped_since_map(device_ids) -> dict:
    """device_id -> ISO timestamp of the FIRST 'trapped' report of the
    current trapped spell (walks back until a non-trapped event breaks
    the run). Drives 'Trapped for …' on cards and the team report."""
    ids = [d for d in device_ids if d]
    if not ids:
        return {}
    rows = await db.status_events.find(
        {"device_id": {"$in": ids}},
        {"_id": 0, "device_id": 1, "status": 1, "recorded_at": 1},
    ).sort("recorded_at", -1).to_list(20000)
    out: dict = {}
    closed: set = set()
    for r in rows:   # newest → oldest
        d = r.get("device_id")
        if d in closed:
            continue
        if r.get("status") == "trapped":
            out[d] = r.get("recorded_at")
        else:
            closed.add(d)
    return out


def _low_battery_lines(latest_events: list[dict]) -> list[str]:
    """Item 4 (2026-08-13): low battery is a countdown to losing contact.

    #216 refinement (Batch 7): stated in plain words, WITHOUT the "20%"
    threshold. The exact number is a technical detail that helps nobody
    reading a public report — and B2 has a hard rule against any "%"
    appearing on the page (press would quote it as "68% rescued" without
    a denominator). The internal threshold is unchanged (20%); only the
    wording is.
    """
    still = [e for e in latest_events if e.get("status") == "trapped"]
    low = [e for e in still
           if isinstance(e.get("battery_pct"), (int, float)) and e["battery_pct"] < 20]
    if not low:
        return []
    n, t = len(low), len(still)
    subject, verb = _subject_of_still_trapped(n, t)
    first = f"{subject} {verb} a phone battery running very low."
    return [first, "We may stop hearing from them."]

def _extraction_lines(latest_events: list[dict]) -> list[str]:
    """Cannot-get-out is a separate axis from injury (2026-06-18).

    Someone can be walking wounded and still need a team with cutting gear.
    Stated as a count with its base on the same line — never a bare
    percentage — and deliberately NOT folded into the severity bands, which
    would either overstate the injury or hide the extraction need.
    """
    still = [e for e in latest_events if e.get("status") == "trapped"]
    stuck = [e for e in still if e.get("needs_extraction")]
    if not stuck:
        return []
    n, t = len(stuck), len(still)
    subject, verb = _subject_of_still_trapped(n, t)
    first = f"{subject} {verb} told us they cannot get out on their own."
    second = ("That person may report only minor injuries. They still need a team to reach them."
              if n == 1 else
              "Some of them report only minor injuries. They still need a team to reach them.")
    return [first, second]


def _parse_iso_or_none(s: Optional[str]) -> Optional[datetime]:
    if not s:
        return None
    try:
        dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt
    except ValueError:
        raise HTTPException(400, f"Invalid ISO 8601 datetime: {s!r}")


async def _fetch_audit_for_export(
    since_iso: Optional[str],
    until_iso: Optional[str],
    kind: Optional[str],
    limit: int,
) -> list[dict]:
    """Shared query used by both CSV and PDF exports.

    Same shape as get_audit_log() but with the notes-visibility switch
    fixed to `is_admin=True` (both callers are already admin-gated) and
    with an inclusive `until` filter.
    """
    since_dt = _parse_iso_or_none(since_iso)
    until_dt = _parse_iso_or_none(until_iso)

    # Sanity-clamp the window. Default: last 7 days.
    if since_dt is None:
        since_dt = datetime.now(timezone.utc) - timedelta(days=7)
    if until_dt is None:
        until_dt = datetime.now(timezone.utc)
    if until_dt < since_dt:
        raise HTTPException(400, "until must be >= since")
    if (until_dt - since_dt).days > MAX_EXPORT_WINDOW_DAYS:
        raise HTTPException(
            400,
            f"Window too wide (max {MAX_EXPORT_WINDOW_DAYS} days). Paginate the request.",
        )

    since_iso_norm = since_dt.isoformat()
    until_iso_norm = until_dt.isoformat()

    events: list[dict] = []

    # Trigger events
    if kind in (None, "trigger"):
        tq: dict = {"created_at": {"$gte": since_iso_norm, "$lte": until_iso_norm}}
        rows = await db.push_events.find(tq, {"_id": 0}).sort("created_at", -1).to_list(limit)
        for r in rows:
            events.append({
                "kind": "trigger",
                "at": r.get("created_at"),
                "idempotency_key": r.get("idempotency_key"),
                "triggered_by": r.get("triggered_by") or "dashboard",
                "magnitude": r.get("magnitude"),
                "recipients_total": r.get("recipients_total") or 0,
                "ios_count": r.get("ios_count") or 0,
                "android_count": r.get("android_count") or 0,
                "delivered": bool(r.get("push_delivered")),
                "error": r.get("push_error"),
            })

    # Status / rescued / reverted events
    want_status   = kind in (None, "status")
    want_rescued  = kind in (None, "rescued")
    want_reverted = kind in (None, "rescue_reverted")
    if want_status or want_rescued or want_reverted:
        sq: dict = {"recorded_at": {"$gte": since_iso_norm, "$lte": until_iso_norm}}
        rows = await db.status_events.find(sq, {"_id": 0}).sort("recorded_at", -1).to_list(limit)
        for r in rows:
            base = {
                "at": r.get("recorded_at") or r.get("updated_at"),
                "device_id": r.get("device_id"),
                "short_code": _short_code(r.get("device_id")),
                "display_name": r.get("display_name"),
                "status": r.get("status"),
                "severity": r.get("severity"),
                "mobility": r.get("mobility"),
                "latitude": r.get("latitude"),
                "longitude": r.get("longitude"),
                "accuracy_m": r.get("accuracy_m"),
                "battery_pct": r.get("battery_pct"),
                "battery_state": r.get("battery_state"),
                "platform": r.get("platform"),
            }
            if r.get("rescue_reverted"):
                if want_reverted:
                    events.append({**base, "kind": "rescue_reverted",
                                   "reverted_by": r.get("reverted_by") or "dashboard"})
            elif r.get("status") == "rescued":
                if want_rescued:
                    events.append({**base, "kind": "rescued",
                                   "rescued_by": r.get("rescued_by") or "dashboard",
                                   "notes": r.get("notes"),
                                   "prior_status": r.get("prior_status"),
                                   "prior_severity": r.get("prior_severity"),
                                   "prior_mobility": r.get("prior_mobility")})
            else:
                if want_status:
                    events.append({**base, "kind": "status"})

    events.sort(key=lambda e: e.get("at") or "", reverse=True)
    events = events[:limit]
    await _backfill_display_names(events)
    # Collision-safe short codes, consistent with /api/devices (item 2).
    _codes = _short_codes_for({e.get("device_id") for e in events if e.get("device_id")})
    for e in events:
        if e.get("device_id"):
            e["short_code"] = _codes.get(e["device_id"]) or e.get("short_code")
    # GDPR data-minimisation: 5 dp (~1 m) is already beyond device accuracy.
    for e in events:
        for k in ("latitude", "longitude"):
            if e.get(k) is not None:
                e[k] = _round5(e[k])
        # A metre reading needs one decimal, not fifteen.
        if e.get("accuracy_m") is not None:
            try:
                e["accuracy_m"] = round(float(e["accuracy_m"]), 1)
            except (TypeError, ValueError):
                pass
    return events


# Column order for CSV. Locked here so the header is stable across
# releases — analytics scripts and archival tooling can rely on it.
_CSV_COLUMNS = [
    "at", "at_simple", "kind",
    # Trigger fields
    "idempotency_key", "triggered_by", "magnitude",
    "recipients_total", "ios_count", "android_count",
    "delivered", "error",
    # Device / status fields
    "device_id", "short_code", "display_name",
    "status", "severity", "mobility",
    "latitude", "longitude", "accuracy_m",
    "battery_pct", "battery_state", "platform",
    # Rescue / revert fields
    "rescued_by", "prior_status", "prior_severity", "prior_mobility", "notes",
    "reverted_by",
]


@api_router.get("/admin/audit-log/export.csv")
async def export_audit_log_csv(
    request: Request,
    since: Optional[str] = Query(default=None, description="ISO 8601 start (inclusive). Default: 7 days ago."),
    until: Optional[str] = Query(default=None, description="ISO 8601 end (inclusive). Default: now."),
    kind: Optional[str] = Query(default=None, description="Optional filter: trigger|status|rescued|rescue_reverted"),
    pseudonymise: bool = Query(default=False, description="Replace operator emails with stable pseudonyms (operator-N)."),
    limit: int = Query(default=MAX_EXPORT_ROWS, ge=1),
):
    """Downloadable CSV of audit events in the given window. Admin-only."""
    # Silent-clamp limit — this is an operator export action, not a query
    # validation surface; getting 500 rows when you asked for 99999 is
    # the correct interpretation of "give me a lot", not a 422 error page.
    limit = min(limit, MAX_EXPORT_ROWS)

    principal = await resolve_principal(
        request,
        request.headers.get("x-admin-token"),
        ADMIN_TRIGGER_PASSWORD,
        db,
    )
    require_role(principal, "admin", "operator")

    events = await _fetch_audit_for_export(since, until, kind, limit)
    if pseudonymise:
        await _pseudonymise_events(events)

    since_dt = _parse_iso_or_none(since) or datetime.now(timezone.utc) - timedelta(days=7)
    until_dt = _parse_iso_or_none(until) or datetime.now(timezone.utc)
    generated_by = principal.get("email", "?")
    if pseudonymise and "@" in generated_by:
        generated_by = await _operator_alias(generated_by)

    # Every row — including the warning and metadata rows — is padded to
    # the full column count so strict parsers (pandas, R) don't reject
    # the file as ragged/malformed. Line endings are CRLF throughout
    # (csv module default is CRLF; the old hand-written warning row used
    # bare LF, producing a mixed-endings file).
    ncols = len(_CSV_COLUMNS)

    def _pad(row: list) -> list:
        return (row + [""] * ncols)[:ncols]

    buf = _io.StringIO()
    writer = _csv.writer(buf, lineterminator="\r\n")
    writer.writerow(_pad([CONFIDENTIALITY_TEXT]))
    # Export metadata — a CSV on its own must be identifiable (the PDF
    # states its window + generator; the CSV now does too).
    writer.writerow(_pad(["export_window_start_utc", since_dt.isoformat()]))
    writer.writerow(_pad(["export_window_end_utc", until_dt.isoformat()]))
    writer.writerow(_pad(["generated_at_utc", datetime.now(timezone.utc).isoformat()]))
    writer.writerow(_pad(["generated_by", generated_by]))
    writer.writerow(_pad(["row_count", str(len(events))]))
    # #272: say which clock each time column uses. The "at" column is the
    # exact instant with its offset; "at_simple" is the same instant in
    # Malta time, which is what the dashboard and the radio log show.
    writer.writerow(_pad([
        "times_note",
        "Column 'at' is the exact time with its offset. "
        "Column 'at_simple' is the same time shown in Malta time.",
    ]))
    # Plain-words coverage line + optional missing-start warning (1c/1d) —
    # someone opening this file next month never saw the screen.
    writer.writerow(_pad(["covers", _covers_line(since_dt, until_dt)]))
    # ── #268: the counts, and what each one leaves out. A CSV is the
    # fallback when the dashboard is down, so it carries the same
    # exclusions the screen does — never a bare figure.
    from people_counts import load_board as _load_board_csv, moved_by_words as _moved_by_words
    _board_csv = await _load_board_csv(db, include_test=False)
    _c = _board_csv.counts
    writer.writerow(_pad(["people_on_working_board", str(_c.total)]))
    writer.writerow(_pad(["waiting_for_an_answer", str(_c.waiting_for_answer)]))
    # #291: the row name said "phone_went_dark", which asserts something
    # about the phone we do not know. All we know is that we asked and
    # heard nothing, and the phone never confirmed our question arrived.
    writer.writerow(_pad(["asked_no_answer_delivery_not_confirmed",
                          str(_c.phone_went_dark)]))
    writer.writerow(_pad(["no_answer", str(_c.no_answer)]))
    writer.writerow(_pad(["not_on_working_board_app_removed", str(_c.app_removed)]))
    writer.writerow(_pad(["not_on_working_board_never_used_app", str(_c.never_used)]))
    writer.writerow(_pad(["not_on_working_board_resolved_by_operator",
                          str(_c.resolved_by_operator)]))
    for _n in _board_csv.notes:
        writer.writerow(_pad(["what_these_numbers_count", _n]))
    for _r in _board_csv.off_board:
        _st = _r.get("record_state") or {}
        writer.writerow(_pad([
            "not_on_working_board_record",
            _r.get("short_code") or "",
            _r.get("display_name") or "",
            _st.get("label") or "",
            _r.get("resolved_at") or _st.get("app_removed_at") or "",
            _moved_by_words(_r),
            _st.get("off_board_reason") or "",
        ]))
    _gap = await _window_gap_warning(since_dt)
    if _gap:
        writer.writerow(_pad(["warning", _gap]))
    writer.writerow(_CSV_COLUMNS)

    for ev in events:
        row = []
        for col in _CSV_COLUMNS:
            if col == "at_simple":
                # Plain second timestamp Excel can sort ("2026-08-06 13:51")
                # alongside the precise ISO column.
                v = ev.get("at")
                if isinstance(v, datetime):
                    v = v.isoformat()
                # #272: the human-readable column is Malta time; the
                # precise ISO column beside it keeps UTC with its offset.
                # Format kept sortable (YYYY-MM-DD HH:MM) so a spreadsheet
                # can still order by it.
                import timefmt as _tf
                _lv = _tf.local(v)
                row.append(_lv.strftime("%Y-%m-%d %H:%M") if _lv else "")
                continue
            v = ev.get(col)
            if isinstance(v, datetime):
                row.append(v.isoformat())
            elif isinstance(v, bool):
                # Excel understands TRUE/FALSE; Python's True/False reads as text.
                row.append("TRUE" if v else "FALSE")
            elif v is None:
                row.append("")
            else:
                row.append(v)
        writer.writerow(row)

    # UTF-8 BOM so Excel decodes the em-dash in the warning row correctly —
    # without it Excel falls back to a legacy codepage and shows mojibake.
    csv_text = "\ufeff" + buf.getvalue()
    filename = f"CONFIDENTIAL-quakeangel-audit-{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}.csv"
    return Response(
        content=csv_text,
        media_type="text/csv; charset=utf-8",
        headers={
            "Content-Disposition": f'attachment; filename="{filename}"',
            "X-Row-Count": str(len(events)),
        },
    )


@api_router.get("/admin/audit-log/export.pdf")
async def export_audit_log_pdf(
    request: Request,
    since: Optional[str] = Query(default=None),
    until: Optional[str] = Query(default=None),
    kind: Optional[str] = Query(default=None),
    pseudonymise: bool = Query(default=False, description="Replace operator emails with stable pseudonyms (operator-N)."),
    limit: int = Query(default=MAX_EXPORT_ROWS, ge=1),
):
    """Downloadable PDF of audit events. Admin-only. Uses ReportLab.

    Landscape A4, monospaced tables, no branding chrome (see design
    note #2 in the section header). Suitable for print + archival.
    """
    # Silent-clamp limit — same rationale as the CSV export.
    limit = min(limit, MAX_EXPORT_ROWS)

    principal = await resolve_principal(
        request,
        request.headers.get("x-admin-token"),
        ADMIN_TRIGGER_PASSWORD,
        db,
    )
    require_role(principal, "admin", "operator")

    events = await _fetch_audit_for_export(since, until, kind, limit)
    if pseudonymise:
        await _pseudonymise_events(events)
    generated_by = principal.get("email", "?")
    if pseudonymise and "@" in generated_by:
        generated_by = await _operator_alias(generated_by)

    # Lazy-import ReportLab so we never pay the ~200ms cold-start cost
    # on non-PDF requests.
    from reportlab.lib import colors
    from reportlab.lib.pagesizes import A4, landscape
    from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
    from reportlab.lib.units import mm
    from reportlab.platypus import (
        SimpleDocTemplate, Paragraph, Spacer, Table, TableStyle, PageBreak,
    )

    styles = getSampleStyleSheet()
    title_style = ParagraphStyle(
        "AuditTitle", parent=styles["Heading1"],
        fontSize=14, spaceAfter=6, textColor=colors.HexColor("#111111"),
    )
    meta_style = ParagraphStyle(
        "AuditMeta", parent=styles["Normal"],
        fontSize=8, textColor=colors.HexColor("#555555"), spaceAfter=10,
    )
    cell_style = ParagraphStyle(
        "AuditCell", parent=styles["Normal"],
        fontSize=7, leading=9, wordWrap="CJK",
    )

    generated_at = datetime.now(timezone.utc).isoformat()
    since_dt = _parse_iso_or_none(since) or datetime.now(timezone.utc) - timedelta(days=7)
    until_dt = _parse_iso_or_none(until) or datetime.now(timezone.utc)

    story: list = [
        Paragraph("Quake Angel — audit log export", title_style),
        # Plain-words absolute coverage (1c) — the document may be read in
        # an inquiry long after "Last 7 days" has lost all meaning.
        Paragraph(_covers_line(since_dt, until_dt), ParagraphStyle(
            "AuditCovers", parent=styles["Normal"], fontSize=9.5,
            textColor=colors.HexColor("#222222"), spaceAfter=4,
        )),
        Paragraph(
            f"Events: {len(events)} &nbsp;·&nbsp; "
            f"Generated: {generated_at} &nbsp;·&nbsp; "
            f"By: {generated_by}",
            meta_style,
        ),
    ]
    _gap = await _window_gap_warning(since_dt)
    if _gap:
        story.insert(2, Paragraph(_html.escape(_gap), ParagraphStyle(
            "AuditWarn", parent=styles["Normal"], fontSize=9,
            textColor=colors.HexColor("#B0141A"), spaceAfter=8,
            fontName="Helvetica-Bold",
        )))

    def _cell(s) -> "Paragraph":
        # Wrap every cell in a Paragraph so long strings wrap instead
        # of overflowing off the page.
        if s is None or s == "":
            return Paragraph("&nbsp;", cell_style)
        return Paragraph(_html.escape(str(s)), cell_style)

    # #272: Malta time on every row, named in the heading. The full ISO
    # timestamp with its offset stays in the CSV's machine columns.
    header = ["Time (Malta)", "Kind", "Actor / Device", "Details", "Location / Meta"]

    data: list = [header]
    for e in events:
        import timefmt as _tf
        at = _tf.human(e.get("at")) if e.get("at") else ""
        kind_str = e.get("kind", "?")
        if kind_str == "trigger":
            actor = e.get("triggered_by") or "?"
            details = (
                f"M={e.get('magnitude') or '?'} · "
                f"{e.get('recipients_total') or 0} devices "
                f"(iOS {e.get('ios_count') or 0}, Android {e.get('android_count') or 0}) · "
                f"{'delivered' if e.get('delivered') else 'FAILED'}"
            )
            if e.get("error"):
                details += f"\nError: {e.get('error')}"
            meta = e.get("idempotency_key") or ""
        elif kind_str == "rescued":
            actor = f"rescued by {e.get('rescued_by') or '?'}"
            details = (
                f"Device {e.get('short_code') or '?'} "
                f"(was {e.get('prior_status') or '?'}/{e.get('prior_severity') or '?'}). "
                f"Notes: {e.get('notes') or 'Not known'}"
            )
            meta = e.get("device_id") or ""
        elif kind_str == "rescue_reverted":
            actor = f"reverted by {e.get('reverted_by') or '?'}"
            details = f"Device {e.get('short_code') or '?'} restored to {e.get('status') or '?'}"
            meta = e.get("device_id") or ""
        else:  # status
            actor = f"{e.get('display_name') or e.get('short_code') or '?'}"
            details = (
                f"{e.get('status') or '?'} / {e.get('severity') or '?'}"
                + (f" · battery {e.get('battery_pct')}%" if e.get("battery_pct") is not None else "")
            )
            lat = e.get("latitude"); lon = e.get("longitude")
            if lat is not None and lon is not None:
                meta = f"{lat:.4f}, {lon:.4f}" + (f" ±{e.get('accuracy_m'):.0f}m" if e.get("accuracy_m") else "")
            else:
                meta = e.get("platform") or ""

        data.append([_cell(at), _cell(kind_str), _cell(actor), _cell(details), _cell(meta)])

    # Column widths: at, kind, actor, details, meta.
    # Sum = 186mm which fits PORTRAIT A4 (210mm − 24mm margins). Portrait
    # because these get printed on default printer settings — landscape
    # PDFs were scaled down to near-illegible on portrait paper (1a).
    col_widths = [28*mm, 18*mm, 34*mm, 76*mm, 30*mm]

    table = Table(data, colWidths=col_widths, repeatRows=1)
    table.setStyle(TableStyle([
        # Opaque white base so the watermark never bleeds through data rows.
        ("BACKGROUND",  (0, 0), (-1, -1), colors.white),
        ("BACKGROUND",  (0, 0), (-1, 0), colors.HexColor("#e6e6ea")),
        ("FONTNAME",    (0, 0), (-1, 0), "Helvetica-Bold"),
        ("FONTSIZE",    (0, 0), (-1, 0), 8),
        ("TEXTCOLOR",   (0, 0), (-1, 0), colors.HexColor("#111111")),
        ("BOTTOMPADDING", (0, 0), (-1, 0), 6),
        ("TOPPADDING",  (0, 0), (-1, 0), 6),
        ("GRID",        (0, 0), (-1, -1), 0.3, colors.HexColor("#c9ccd2")),
        ("VALIGN",      (0, 0), (-1, -1), "TOP"),
        ("LEFTPADDING", (0, 0), (-1, -1), 4),
        ("RIGHTPADDING", (0, 0), (-1, -1), 4),
        ("TOPPADDING",  (0, 1), (-1, -1), 3),
        ("BOTTOMPADDING", (0, 1), (-1, -1), 3),
        # Alternate row shading for readability on print.
        *[
            ("BACKGROUND", (0, i), (-1, i), colors.HexColor("#fafbfc"))
            for i in range(2, len(data), 2)
        ],
    ]))
    story.append(table)

    if not events:
        story.append(Spacer(0, 20))
        story.append(Paragraph("No events in the specified window.", meta_style))

    buf = _io.BytesIO()
    doc = SimpleDocTemplate(
        buf,
        pagesize=A4,   # PORTRAIT — printer-default friendly (1a, 2026-08-13)
        leftMargin=12*mm, rightMargin=12*mm,
        # Top+bottom margins expanded to clear the 30pt confidentiality
        # banner + footer drawn by _pdf_confidentiality_onpage.
        topMargin=22*mm, bottomMargin=15*mm,
        title="Quake Angel Audit Log",
        author=principal.get("email", ""),
    )
    # A2 (2026-08-17): audit PDF carries the same header marks as the
    # casualty reports — permanent Quake Angel logo, labelled partner
    # logo when a real one is configured.
    _onpage = _make_confidential_onpage(await _get_logo_image_reader())
    doc.build(story, onFirstPage=_onpage, onLaterPages=_onpage)
    pdf_bytes = buf.getvalue()

    filename = f"CONFIDENTIAL-quakeangel-audit-{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}.pdf"
    return Response(
        content=pdf_bytes,
        media_type="application/pdf",
        headers={
            "Content-Disposition": f'attachment; filename="{filename}"',
            "X-Row-Count": str(len(events)),
        },
    )



# ---------- Dual casualty reports: B1 Operational / B2 Public -----------
#
# Two PDF endpoints producing reports of who checked in with what status
# during a report window. They exist as a PAIR — building them
# separately would risk drift where the same real-world event produces
# inconsistent counts on operational vs public reports.
#
# B1 (Operational) — internal dispatch document. Contains everything
#   the Civil Protection team needs to allocate resources: names,
#   short codes, exact GPS + accuracy, battery, platform, notes,
#   status timeline.
#
# B2 (Public) — external comms document. **Aggregate numbers only. No
#   names, no initials, no short codes, no per-person location, no
#   per-person status — not even for rescued people.** See
#   `/app/memory/PRD.md` "Legal / privacy locks" section: this is a
#   GDPR + next-of-kin policy, not a UI preference. Any change that
#   would make B2 identifiable requires legal review before merge.
#
# Both reports use the same underlying data query so the counts on
# B2 are always internally consistent with the detail in B1. That's
# enforced by `_gather_devices_in_report_window()` — both endpoints
# call it, neither queries the DB independently.

async def _gather_devices_in_report_window(
    since_iso: Optional[str],
    until_iso: Optional[str],
) -> tuple[list[dict], datetime, datetime]:
    """Fetch every device with any status_event in [since, until], collapsed
    to the LATEST event per device (which is what the report describes).

    Returns:
      (list_of_latest_events, resolved_since_dt, resolved_until_dt)

    Each list item has extra fields:
      - `first_event_at`: when THIS device first reported in the window
      - `event_count`: how many status_events they logged in the window
    So B1 can show "checked in N times, latest at X" per row.
    """
    since_dt = _parse_iso_or_none(since_iso)
    until_dt = _parse_iso_or_none(until_iso)
    if since_dt is None:
        since_dt = datetime.now(timezone.utc) - timedelta(hours=24)
    if until_dt is None:
        until_dt = datetime.now(timezone.utc)
    if until_dt < since_dt:
        raise HTTPException(400, "until must be >= since")
    if (until_dt - since_dt).days > MAX_EXPORT_WINDOW_DAYS:
        raise HTTPException(
            400,
            f"Window too wide (max {MAX_EXPORT_WINDOW_DAYS} days).",
        )

    # `recorded_at` is stored as an ISO string in status_events (see the
    # sample rows in mongo) so we compare against ISO strings, not
    # datetime objects. String comparison of ISO-8601 UTC values is
    # lexicographically correct.
    q = {"recorded_at": {"$gte": since_dt.isoformat(), "$lte": until_dt.isoformat()}}
    rows = await db.status_events.find(q, {"_id": 0}).sort("recorded_at", -1).to_list(5000)

    # Batch 7 A2/D1: test/synthetic devices are filtered here — the ONE
    # place — so both the aggregate table (compute_counts) and the
    # narrative (which reads collapsed events + raw rows) agree about
    # who's included. Without this, the table said 0 rescued while the
    # narrative said "9 found by a rescue team" for the same window
    # (Pattern 2, caught by test_b2_rescued_narrative_equals_table).
    from deps import is_test_device
    rows = [r for r in rows if not is_test_device(r)]

    # Collapse to latest-per-device.
    by_device: dict[str, dict] = {}
    for r in rows:
        did = r.get("device_id")
        if not did:
            continue
        if did not in by_device:
            by_device[did] = {**r, "event_count": 1, "first_event_at": r.get("recorded_at")}
        else:
            by_device[did]["event_count"] += 1
            # `first_event_at` tracks the oldest event; since we're
            # sorted newest-first, every subsequent hit is older.
            by_device[did]["first_event_at"] = r.get("recorded_at")

    # Reverted rescues are marked with `rescue_reverted=True` on a
    # follow-up event. For the report we want the *effective current
    # status* — which is exactly what the latest event says, since
    # a revert reinstates the prior_status on that same event row.
    # `rows` (uncollapsed) is returned too — the response-over-time chart
    # needs every event, not just the latest per device.
    return list(by_device.values()), since_dt, until_dt, rows


def _bucket_by_status(events: list[dict]) -> dict:
    """Aggregate counts by status/severity for both reports.

    B2 exposes only the totals from here; B1 shows totals AND names.
    """
    total = len(events)
    safe = trapped = rescued = 0
    trapped_red = trapped_yellow = trapped_green = trapped_unknown = 0
    for e in events:
        st = e.get("status")
        if st == "safe":
            safe += 1
        elif st == "rescued":
            rescued += 1
        elif st == "trapped":
            trapped += 1
            sev = (e.get("severity") or "").lower()
            if sev == "red":
                trapped_red += 1
            elif sev == "yellow":
                trapped_yellow += 1
            elif sev == "green":
                trapped_green += 1
            else:
                trapped_unknown += 1
    return {
        "total_devices": total,
        "safe": safe,
        "trapped": trapped,
        "rescued": rescued,
        "trapped_red": trapped_red,
        "trapped_yellow": trapped_yellow,
        "trapped_green": trapped_green,
        "trapped_unknown": trapped_unknown,
        "awaiting_rescue": trapped,   # semantic alias — "trapped" == "needs help / awaiting rescue"
    }


def _pdf_common_setup():
    """Import ReportLab + build shared paragraph styles. Called by both
    B1 and B2 so styling stays consistent (a subtle wording gap between
    the two reports would be more suspicious than obvious)."""
    from reportlab.lib import colors
    from reportlab.lib.pagesizes import A4, landscape
    from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
    from reportlab.lib.units import mm
    from reportlab.platypus import (
        SimpleDocTemplate, Paragraph, Spacer, Table, TableStyle,
    )
    styles = getSampleStyleSheet()
    return {
        "colors": colors, "A4": A4, "landscape": landscape,
        "styles": styles, "ParagraphStyle": ParagraphStyle, "mm": mm,
        "SimpleDocTemplate": SimpleDocTemplate, "Paragraph": Paragraph,
        "Spacer": Spacer, "Table": Table, "TableStyle": TableStyle,
    }


@api_router.get("/admin/casualty-report/operational.pdf")
async def casualty_report_operational_pdf(
    request: Request,
    since: Optional[str] = Query(default=None, description="ISO 8601 start; default 24h ago."),
    until: Optional[str] = Query(default=None, description="ISO 8601 end; default now."),
    pseudonymise: bool = Query(default=False, description="Replace operator emails with stable pseudonyms (operator-N)."),
    detail: str = Query(default="summary", pattern="^(full|summary)$",
                        description="'summary' (default) is aggregate + timeline only; "
                                    "'full' adds one table row per device (issue #130: "
                                    "the multi-page table is opt-in, never the default)."),
):
    """B1 — Operational casualty report. Admin+operator gated.

    Full-detail internal document. Contains names, short codes, exact
    GPS, notes, timeline. Suitable for Civil Protection dispatch.
    """
    principal = await resolve_principal(
        request,
        request.headers.get("x-admin-token"),
        ADMIN_TRIGGER_PASSWORD,
        db,
    )
    require_role(principal, "admin", "operator")

    events, since_dt, until_dt, raw_rows = await _gather_devices_in_report_window(since, until)
    from people_counts import compute_counts
    current_counts = await compute_counts(db, include_test=False)
    counts = _bucket_by_status(events)   # window-bounded, used by narrative
    authority = await _get_authority_name()   # for the footer copy — never hard-coded
    # #218 (Batch 7): B1's closing line was still asserting cooperation
    # even after D2 was applied to B2. Every rendered use of {authority}
    # now branches on _cooperation_ok. Sites: 4 total (B1 close + B2
    # issuer + B2 neutral + B2 footer), all gated as of this batch.
    _cooperation_ok = await _get_authority_cooperation_claim()
    await _backfill_display_names(events)
    generated_by = principal.get("email", "?")
    if pseudonymise and "@" in generated_by:
        generated_by = await _operator_alias(generated_by)

    r = _pdf_common_setup()
    colors, mm = r["colors"], r["mm"]
    Paragraph, Spacer, Table, TableStyle = r["Paragraph"], r["Spacer"], r["Table"], r["TableStyle"]
    styles, PS = r["styles"], r["ParagraphStyle"]

    title_style = PS("T", parent=styles["Heading1"], fontSize=14, spaceAfter=6, textColor=colors.HexColor("#111"))
    meta_style  = PS("M", parent=styles["Normal"], fontSize=8, textColor=colors.HexColor("#555"), spaceAfter=8)
    h2_style    = PS("H2", parent=styles["Heading2"], fontSize=11, spaceAfter=4, textColor=colors.HexColor("#111"))
    cell_style  = PS("C", parent=styles["Normal"], fontSize=7, leading=9, wordWrap="CJK")
    cell_bold   = PS("CB", parent=cell_style, fontName="Helvetica-Bold")
    footer_style = PS("F", parent=styles["Normal"], fontSize=7, textColor=colors.HexColor("#8a1a1a"), spaceBefore=10)

    def cell(s, style=cell_style):
        if s is None or s == "":
            return Paragraph("&nbsp;", style)
        return Paragraph(_html.escape(str(s)), style)

    story = [
        Paragraph("Team report — operational casualty report", title_style),
        Paragraph(
            f"CONFIDENTIAL — INTERNAL USE ONLY. Contains personally identifiable information. "
            f"Not for public distribution.",
            PS("CI", parent=meta_style, textColor=colors.HexColor("#8a1a1a"), fontName="Helvetica-Bold"),
        ),
        # Plain-words absolute coverage (1c).
        Paragraph(_covers_line(since_dt, until_dt), PS(
            "B1Covers", parent=styles["Normal"], fontSize=9.5,
            textColor=colors.HexColor("#222222"), spaceAfter=4,
        )),
        # D1 (Batch 7): this line reports how many devices reported
        # DURING the window (activity in the period). Distinct from the
        # aggregate table below which is CURRENT state. Kept the "during"
        # figure here because a team-report header is about "how much
        # activity in this window" — the current-state number lives in
        # its own labelled section below.
        # #228 (Batch 7): "device" replaced with "person"/"people" wherever
        # a human is meant. The two figures below count PEOPLE (one row
        # per device, one device per person by design) — say so plainly.
        # Wording approved by Paul, message 2026-08-19.
        Paragraph(
            (
                f"1 person checked in during this period."
                if counts['total_devices'] == 1 else
                f"{counts['total_devices']} people checked in during this period."
            )
            + " &nbsp;·&nbsp; "
            + (
                f"1 person is on the system in total."
                if current_counts.total == 1 else
                f"{current_counts.total} people are on the system in total."
            )
            + f" &nbsp;·&nbsp; Generated: {_fmt_dt_plain(datetime.now(timezone.utc))}"
            + f" &nbsp;·&nbsp; By: {generated_by}",
            meta_style,
        ),
        # D1 (Batch 7): the aggregate table and the response-over-time
        # section count different things — one is CURRENT state, one is
        # everything that happened DURING the window. Correctness alone
        # isn't enough; a tired reader has to know which is which.
        Paragraph("Where things stand right now", h2_style),
    ]
    _gap = await _window_gap_warning(since_dt)
    if _gap:
        story.insert(3, Paragraph(_html.escape(_gap), PS(
            "B1Warn", parent=styles["Normal"], fontSize=9,
            textColor=colors.HexColor("#B0141A"), spaceAfter=8,
            fontName="Helvetica-Bold",
        )))

    # D1 (Batch 7): rows sum to Total. `not_responding` and `unknown`
    # are separate labelled rows so nobody a device_status row represents
    # is invisible in the aggregate.
    summary_data = [
        ["", "Count"],
        ["Safe", str(current_counts.safe)],
        ["Trapped / needs help (red)",    str(current_counts.trapped_red)],
        ["Trapped / needs help (yellow)", str(current_counts.trapped_yellow)],
        ["Trapped / needs help (green)",  str(current_counts.trapped_green)],
        ["Trapped — severity not set",    str(current_counts.trapped_unknown)],
        ["Rescued",                       str(current_counts.rescued)],
        ["Not responding",                str(current_counts.not_responding)],
        ["Status not yet reported",       str(current_counts.unknown)],
        ["Total",                         str(current_counts.total)],
    ]
    # ── #268 / #283 (Paul): "Every number must say what it counts and
    # what it leaves out." These rows are indented and do NOT add into the
    # Total above. The first three describe SILENCE among the people
    # already counted above — silence is not a status, so a person who
    # reported safe an hour ago and has said nothing since is quiet AND
    # safe, and appears in both places. Paul read the old heading ("inside
    # the rows above") as a breakdown of the "Not responding" line
    # directly above it, and 0 + 1 + 6 = 7 flatly contradicted the 1
    # printed there. The heading now names the total and says out loud
    # that these are not extra people.
    _quiet = (current_counts.waiting_for_answer + current_counts.no_answer
              + current_counts.phone_went_dark)
    summary_data[-1:-1] = [
        [f"Gone quiet — {_quiet} of the {current_counts.total} above, "
         "not extra people:", ""],
        ["  — waiting for an answer",     str(current_counts.waiting_for_answer)],
        ["  — got our question, no answer", str(current_counts.no_answer)],
        ["  — we asked, no answer, arrival not confirmed",
         str(current_counts.phone_went_dark)],
        ["Not on the working board — NOT in the rows above:", ""],
        ["  — app removed from this phone", str(current_counts.app_removed)],
        ["  — never used the app",          str(current_counts.never_used)],
        ["  — resolved by an operator",     str(current_counts.resolved_by_operator)],
    ]
    summary_tbl = Table(summary_data, colWidths=[70*mm, 30*mm])
    summary_tbl.setStyle(TableStyle([
        # Opaque white base so the watermark never bleeds through data rows.
        ("BACKGROUND", (0,0), (-1,-1), colors.white),
        ("BACKGROUND", (0,0), (-1,0), colors.HexColor("#e6e6ea")),
        ("FONTNAME",   (0,0), (-1,0), "Helvetica-Bold"),
        ("FONTSIZE",   (0,0), (-1,-1), 8),
        ("GRID",       (0,0), (-1,-1), 0.3, colors.HexColor("#c9ccd2")),
        ("BACKGROUND", (0,-1), (-1,-1), colors.HexColor("#f0f2f6")),
        ("FONTNAME",   (0,-1), (-1,-1), "Helvetica-Bold"),
    ]))
    story.append(summary_tbl)
    story.append(Spacer(0, 8))

    # #268: the exclusions, in words, immediately under the numbers they
    # qualify. The printed report is the fallback when the dashboard is
    # down, so it must carry the same wording the screen does.
    from people_counts import counts_notes as _counts_notes
    _note_style_b1 = PS("B1CountNote", parent=styles["Normal"], fontSize=8.5,
                        leading=11, textColor=colors.HexColor("#444444"),
                        spaceAfter=2)
    for _n in _counts_notes(current_counts):
        story.append(Paragraph(_html.escape(_n), _note_style_b1))
    story.append(Spacer(0, 6))

    # #216 (Batch 7): the present-tense narrative sits DIRECTLY UNDER the
    # aggregate table it explains. Same source (compute_counts + latest
    # events), same tense (present). This is where "1 person is still
    # waiting for help right now" is allowed to appear — never below the
    # window heading further down.
    _plain_style_b1 = PS("PL", parent=styles["Normal"], fontSize=9,
                         leading=13, spaceAfter=1)
    _now_lines = _current_state_narrative(current_counts, events)
    for _line in _now_lines:
        story.append(Paragraph(_html.escape(_line), _plain_style_b1))
    story.append(Spacer(0, 8))

    # D1 (Batch 7): the transition sentence. Explicit about what the
    # two sections count differently so a reader never concludes the
    # document contradicts itself.
    story.append(Paragraph(
        "These count different things. The table above is the situation right now. "
        "The section below covers everything that happened during the period.",
        PS("B1SepNote", parent=styles["Normal"], fontSize=9, leading=12,
           textColor=colors.HexColor("#444444"), spaceAfter=6),
    ))
    story.append(Spacer(0, 4))

    # ── What happened during this window ────────────────────────────
    # Chart + PAST-TENSE narrative only. Any present-tense claim
    # ("still", "yet", "currently", "is trapped") is a #216 regression
    # and belongs above, next to the aggregate table.
    story.append(Paragraph("What happened during this window", h2_style))
    plain_style = PS("PL", parent=styles["Normal"], fontSize=9, leading=13, spaceAfter=1)
    buckets, _hourly = _bucket_timeline(raw_rows, since_dt, until_dt)
    if any(b["trapped"] or b["safe"] or b["rescued"] for b in buckets):
        story.append(_timeline_chart(buckets, 182 * mm, 55 * mm))
        story.append(Spacer(0, 4))
        story.append(Paragraph(CHART_CAPTION, PS(
            "ChartCap", parent=styles["Normal"], fontSize=7.5, leading=10,
            textColor=colors.HexColor("#444444"))))
        story.append(Spacer(0, 6))
    from reportlab.platypus import KeepTogether as _KT
    _narrative_lines = _window_narrative(raw_rows, events, counts)
    story.append(_KT([
        Paragraph(_html.escape(line), plain_style)
        for line in _narrative_lines
    ]))
    story.append(Spacer(0, 10))

    if detail == "full":
        story.append(Paragraph("Per-device detail", h2_style))
        header = ["Latest status", "Sev", "Name / code", "Latest at (Malta)", "Location", "Battery", "Platform", "Notes"]

        def _sort_key(e):
            # Sort: trapped > rescued > safe; within status, red > yellow > green > unknown; then newest first.
            rank_status = {"trapped": 0, "rescued": 1, "safe": 2}.get(e.get("status"), 3)
            rank_sev = {"red": 0, "yellow": 1, "green": 2}.get((e.get("severity") or "").lower(), 3)
            # Recorded_at is an ISO 8601 string — lexicographic desc sort by
            # negating via a tuple companion is cleanest without parsing.
            return (rank_status, rank_sev, "" if e.get("recorded_at") is None else e["recorded_at"])

        # Sort by (rank_status, rank_sev, recorded_at) ascending on the ranks
        # and DESCENDING on recorded_at (newest first within each group).
        events_sorted = sorted(events, key=_sort_key)
        events_sorted.sort(key=lambda e: e.get("recorded_at") or "", reverse=True)
        events_sorted.sort(key=lambda e: (
            {"trapped": 0, "rescued": 1, "safe": 2}.get(e.get("status"), 3),
            {"red": 0, "yellow": 1, "green": 2}.get((e.get("severity") or "").lower(), 3),
        ))
        data = [header]
        _b1_codes = _short_codes_for({e.get("device_id") for e in events_sorted if e.get("device_id")})
        _b1_trapped_since = await _trapped_since_map(
            [e.get("device_id") for e in events_sorted if e.get("status") == "trapped"]
        )
        _now_utc = datetime.now(timezone.utc)
        for e in events_sorted:
            lat = e.get("latitude"); lon = e.get("longitude")
            loc = "Not known"
            if lat is not None and lon is not None:
                loc = f"{_round5(lat)}, {_round5(lon)}"
                if e.get("accuracy_m"):
                    loc += f" ±{e.get('accuracy_m'):.0f}m"
            batt = "Not known"
            if e.get("battery_pct") is not None:
                batt = f"{e.get('battery_pct')}%"
                if e.get("battery_state"):
                    batt += f" ({e.get('battery_state')})"
            name = e.get("display_name") or "(anonymous)"
            code = _b1_codes.get(e.get("device_id")) or _short_code(e.get("device_id"))
            # Escape the VALUES, not the markup — passing the composed string
            # through cell() double-escaped it and printed literal "<br/>"
            # tags on the PDF (bug #3.1, 2026-08-12).
            name_para = Paragraph(
                f"{_html.escape(str(name))}<br/>"
                f"<font size=6 color='#666'>{_html.escape(str(code or ''))}</font>",
                cell_style,
            )

            status_display = e.get("status") or "?"
            if e.get("rescue_reverted"):
                status_display = f"{status_display} (reverted)"
            # 'Trapped for …' in words (item 3) — the operationally critical
            # figure, same wording as the dashboard card.
            _ts = _b1_trapped_since.get(e.get("device_id")) if e.get("status") == "trapped" else None
            if _ts:
                try:
                    _tdt = datetime.fromisoformat(str(_ts).replace("Z", "+00:00"))
                    if _tdt.tzinfo is None:
                        _tdt = _tdt.replace(tzinfo=timezone.utc)
                    status_para = Paragraph(
                        f"<b>{_html.escape(status_display)}</b><br/>"
                        f"<font size=6 color='#666'>trapped for {_html.escape(_duration_words(_now_utc - _tdt))}</font>",
                        cell_style,
                    )
                except (ValueError, TypeError):
                    status_para = cell(status_display, cell_bold)
            else:
                status_para = cell(status_display, cell_bold)

            data.append([
                status_para,
                cell((e.get("severity") or "Not known")),
                name_para,
                cell(_fmt_when(e.get("recorded_at"))),
                cell(loc),
                cell(batt),
                cell(e.get("platform") or "Not known"),
                cell(e.get("notes") or "Not known"),
            ])

        # Column widths sum to 186mm — PORTRAIT A4 (210mm − 24mm margins).
        col_widths = [17*mm, 10*mm, 28*mm, 27*mm, 34*mm, 17*mm, 14*mm, 39*mm]
        tbl = Table(data, colWidths=col_widths, repeatRows=1)
        tbl.setStyle(TableStyle([
            # Opaque white base so the watermark never bleeds through rows.
            ("BACKGROUND",   (0,0), (-1,-1), colors.white),
            ("BACKGROUND",   (0,0), (-1,0), colors.HexColor("#e6e6ea")),
            ("FONTNAME",     (0,0), (-1,0), "Helvetica-Bold"),
            ("FONTSIZE",     (0,0), (-1,0), 7),
            ("GRID",         (0,0), (-1,-1), 0.3, colors.HexColor("#c9ccd2")),
            ("VALIGN",       (0,0), (-1,-1), "TOP"),
            ("LEFTPADDING",  (0,0), (-1,-1), 3),
            ("RIGHTPADDING", (0,0), (-1,-1), 3),
        ]))
        story.append(tbl)
    else:
        story.append(Paragraph(
            "This is the short version — totals only. For a list showing "
            "each person separately, download the full version.",
            meta_style,
        ))

    if counts["total_devices"] == 0:
        story.append(Spacer(0, 20))
        story.append(Paragraph("No one reported in this period.", meta_style))

    # ── #268 appendix: records NOT on the working board ───────────────
    # "Removed records go to a clearly labelled area an operator can open
    #  — not hidden, not deleted. Anyone can see what was moved, when,
    #  and why." The printed report is the fallback when the dashboard is
    #  down, so the same list has to be here.
    from people_counts import load_board as _load_board, moved_by_words as _moved_by_words
    _b = await _load_board(db, include_test=False)
    story.append(Spacer(0, 14))
    story.append(Paragraph("Records not on the working board", h2_style))
    if not _b.off_board:
        story.append(Paragraph(
            "None. Every record is on the working board.", meta_style))
    else:
        story.append(Paragraph(
            "These records are deliberately not in the working list above. "
            "Nothing has been deleted. Each line says what it is, when it "
            "moved and who moved it.", meta_style))
        _off_data = [["Code", "Name", "What it is", "When", "Moved by", "Why"]]
        for _r in _b.off_board:
            _st = _r.get("record_state") or {}
            _when = _r.get("resolved_at") or _st.get("app_removed_at")
            _when_dt = _parse_iso_or_none(_when) if _when else None
            _off_data.append([
                cell(_r.get("short_code") or "Not known"),
                cell(_r.get("display_name") or "Not known"),
                cell(_st.get("label") or "Not known"),
                cell(_fmt_when(_when)),
                cell(_moved_by_words(_r)),
                cell(_st.get("off_board_reason") or "Not known"),
            ])
        _off_tbl = Table(_off_data, colWidths=[18*mm, 24*mm, 36*mm, 34*mm, 34*mm, 40*mm],
                         repeatRows=1)
        _off_tbl.setStyle(TableStyle([
            ("BACKGROUND",   (0,0), (-1,-1), colors.white),
            ("BACKGROUND",   (0,0), (-1,0), colors.HexColor("#e6e6ea")),
            ("FONTNAME",     (0,0), (-1,0), "Helvetica-Bold"),
            ("FONTSIZE",     (0,0), (-1,-1), 7),
            ("GRID",         (0,0), (-1,-1), 0.3, colors.HexColor("#c9ccd2")),
            ("VALIGN",       (0,0), (-1,-1), "TOP"),
        ]))
        story.append(_off_tbl)

    story.append(Paragraph(
        "END OF TEAM REPORT — For public communications, use the \u201Csafe to share\u201D public report which "
        "exposes aggregate numbers only. Do NOT share this document publicly, with press, or with "
        f"next-of-kin before {authority} has completed formal notification.",
        footer_style,
    ))

    buf = _io.BytesIO()
    doc = r["SimpleDocTemplate"](
        buf,
        pagesize=r["A4"],   # PORTRAIT — printer-default friendly (1a, 2026-08-13)
        leftMargin=12*mm, rightMargin=12*mm,
        # Expanded top+bottom margins to clear the 30pt confidentiality
        # banner + footer drawn by _pdf_confidentiality_onpage.
        topMargin=22*mm, bottomMargin=15*mm,
        title="Quake Angel Team Report (operational casualty report)",
        author=principal.get("email", ""),
    )
    _onpage = _make_confidential_onpage(await _get_logo_image_reader())
    doc.build(story, onFirstPage=_onpage, onLaterPages=_onpage)
    _suffix = "-summary" if detail == "summary" else ""
    return Response(
        content=buf.getvalue(),
        media_type="application/pdf",
        headers={
            # Plain-language filename (issue #133) — "team-report", not "B1".
            "Content-Disposition": f'attachment; filename="CONFIDENTIAL-quakeangel-team-report{_suffix}-{datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")}.pdf"',
            # X-Row-Count reports the AGGREGATE TABLE'S total (current
            # state, from compute_counts), so it matches what the header
            # of the report shows and what the B2 public report's total
            # shows. Before Batch 7 D1 this used counts["total_devices"]
            # (window-scoped), which meant B1's header disagreed with
            # the aggregate table on the page below it — same bug class
            # the D1 rewrite fixed for the reader.
            "X-Row-Count": str(current_counts.total),
            # X-Report-Kind lets the dashboard sanity-check that clicking "B1"
            # actually returned a B1 (defense-in-depth against endpoint mixup).
            "X-Report-Kind": "B1-operational",
        },
    )


@api_router.get("/admin/casualty-report/public.pdf")
async def casualty_report_public_pdf(
    request: Request,
    since: Optional[str] = Query(default=None, description="ISO 8601 start; default 24h ago."),
    until: Optional[str] = Query(default=None, description="ISO 8601 end; default now."),
):
    """B2 — Public casualty report. Admin+operator gated.

    **Aggregate counts only.** This PDF is safe to share externally
    (press briefings, family info line, public dashboards). It contains:
        - How many people checked in safe
        - How many people are awaiting rescue (with severity breakdown)
        - How many people have been rescued
        - No names, no initials, no codes, no per-person location, no
          per-person status. Not for rescued people either.

    Legal + next-of-kin policy locked with Paul 2026-08-07 (see PRD
    "Legal / privacy locks" section). Any change to identifiability
    requires legal review, in writing, before merge.
    """
    principal = await resolve_principal(
        request,
        request.headers.get("x-admin-token"),
        ADMIN_TRIGGER_PASSWORD,
        db,
    )
    require_role(principal, "admin", "operator")

    events, since_dt, until_dt, raw_rows = await _gather_devices_in_report_window(since, until)
    # A2/D1 (Batch 7): aggregate reads CURRENT state, matches live board.
    from people_counts import compute_counts
    current_counts = await compute_counts(db, include_test=False)
    counts = _bucket_by_status(events)
    authority = await _get_authority_name()   # configurable, never hard-coded
    # D2 (Batch 7): default to neutral "Authority: [name]" wording. Any
    # phrasing that implies operational partnership or next-of-kin
    # notification is behind an explicit admin setting (see PRD #86).
    _cooperation_ok = await _get_authority_cooperation_claim()

    # Belt-and-braces assertion. If a future refactor accidentally left
    # a per-person field in `counts`, this assertion refuses to render
    # the report. Fail-loud is the correct behavior — the legal lock
    # matters more than uptime of this specific endpoint.
    # Whitelist of keys expected to be safe (all integer counts):
    _B2_SAFE_KEYS = {
        "total_devices", "safe", "trapped", "rescued",
        "trapped_red", "trapped_yellow", "trapped_green", "trapped_unknown",
        "awaiting_rescue",
    }
    _leaked = [k for k in counts if k not in _B2_SAFE_KEYS]
    if _leaked:
        raise HTTPException(
            500,
            "B2 aggregate structure changed unexpectedly. Refusing to render "
            "for privacy safety. See PRD 'Legal / privacy locks' section. "
            f"Unexpected keys: {_leaked}",
        )

    r = _pdf_common_setup()
    colors, mm = r["colors"], r["mm"]
    Paragraph, Spacer, Table, TableStyle = r["Paragraph"], r["Spacer"], r["Table"], r["TableStyle"]
    styles, PS = r["styles"], r["ParagraphStyle"]

    # #126 (Batch 7): every style below was measured so the whole report
    # sits on a single A4 page even under a realistic seeded window.
    # Test lock: `test_b2_fits_on_one_page`. Any addition to B2 must
    # earn its space back out of these knobs, not silently push a second
    # sheet through a newsroom printer.
    title_style = PS("T", parent=styles["Heading1"], fontSize=15, spaceAfter=5, textColor=colors.HexColor("#111"))
    meta_style  = PS("M", parent=styles["Normal"], fontSize=9, textColor=colors.HexColor("#444"), spaceAfter=6)
    h2_style    = PS("H2", parent=styles["Heading2"], fontSize=12, spaceAfter=4, textColor=colors.HexColor("#111"))
    body_style  = PS("B", parent=styles["Normal"], fontSize=10, leading=12, spaceAfter=2)  # #268: 13/3 -> 12/2, headroom for the exclusions line
    footer_style = PS("F", parent=styles["Normal"], fontSize=8, textColor=colors.HexColor("#666"), spaceBefore=8)

    story = [
        Paragraph("Public status report", title_style),
        # Plain-words absolute coverage (1c) — press and families read this
        # long after "the last 24 hours" has lost its meaning.
        Paragraph(_covers_line(since_dt, until_dt), PS(
            "B2Covers", parent=body_style, fontSize=10, spaceAfter=4,
        )),
        Paragraph(
            f"Issued: {_fmt_dt_plain(datetime.now(timezone.utc))}",
            meta_style,
        ),
        # Issuer line (3a, 2026-08-13): names the SYSTEM and authority so
        # the document is attributable to Quake Angel's official output —
        # never the individual operator (no personal email on a document
        # going to press and families).
        #
        # D2 (Batch 7): neutral-by-default. Cooperation phrasing is gated
        # on the admin setting `authority_cooperation_claim` — off by
        # default. Never asserts partnership silently just because a name
        # is present in the config.
        Paragraph(
            (
                f"Issued by the Quake Angel emergency response system, "
                f"in cooperation with {authority}."
            ) if _cooperation_ok else (
                f"Issued by the Quake Angel emergency response system. "
                f"Authority: {authority}."
            ),
            meta_style,
        ),
        # D1 (Batch 7): explicit heading for the current-state table.
        # Distinguishes it from the timeline below so a tired reader
        # never concludes the document contradicts itself.
        Paragraph("Where things stand right now", h2_style),
    ]
    _gap = await _window_gap_warning(since_dt)
    if _gap:
        story.insert(3, Paragraph(_html.escape(_gap), PS(
            "B2Warn", parent=body_style, fontSize=10,
            textColor=colors.HexColor("#B0141A"), fontName="Helvetica-Bold", spaceAfter=6,
        )))

    # Aggregate table — ONLY counts. Deliberately no "who" column, no
    # region column, no timestamp-of-latest-event column.
    #
    # D1 (Batch 7): the rows MUST sum to the Total. Previously not_responding
    # and unknown existed on the system but were absent from the table, so
    # a reader could sum "Safe + Rescued + Awaiting" and get less than the
    # Total below and reasonably conclude the report contradicted itself.
    # Every row a person could occupy is now present. Sub-rows (critical /
    # moderate / minor / severity not set) are indented and NOT counted in
    # the running sum — they break out the "awaiting rescue" total above.
    summary_data = [
        ["", ""],
        ["People checked in as safe",                str(current_counts.safe)],
        ["People rescued",                           str(current_counts.rescued)],
        ["People awaiting rescue",                   str(current_counts.needs_help)],
        ["  — of which reporting critical injury",   str(current_counts.trapped_red)],
        ["  — of which reporting moderate injury",   str(current_counts.trapped_yellow)],
        ["  — of which reporting minor injury",      str(current_counts.trapped_green)],
        ["  — of which severity not yet reported",   str(current_counts.trapped_unknown)],
        # Silence-is-information (batch 5 lock): people the system has
        # not heard from lately are ALWAYS a distinct row, never merged
        # into "unknown" and never invisible.
        ["People not responding",                    str(current_counts.not_responding)],
        ["People with status not yet reported",      str(current_counts.unknown)],
        ["Total people accounted for",               str(current_counts.total)],
    ]
    # #268: B2 is a strict ONE-PAGER (test-enforced, #126) and it is read
    # by families and journalists, so the exclusions go in as sentences
    # under the table rather than as extra rows. The operational report
    # (B1) carries the full indented breakdown.
    summary_tbl = Table(summary_data, colWidths=[110*mm, 30*mm])
    summary_tbl.setStyle(TableStyle([
        # Opaque white base so nothing bleeds through on print.
        ("BACKGROUND", (0,0), (-1,-1), colors.white),
        ("FONTNAME",   (0,0), (-1,-1), "Helvetica"),
        ("FONTSIZE",   (0,0), (-1,-1), 10),
        ("BACKGROUND", (0,-1), (-1,-1), colors.HexColor("#f0f2f6")),
        ("FONTNAME",   (0,-1), (-1,-1), "Helvetica-Bold"),
        # #126 (Batch 7): row padding trimmed from 5 to 3. The table is
        # still easy to read (10pt Helvetica, alternating rows above and
        # below a single hairline) and gains ~14mm back for the rest of
        # the report to sit on one page.
        ("BOTTOMPADDING", (0,0), (-1,-1), 3),
        ("TOPPADDING",    (0,0), (-1,-1), 3),
        ("LINEABOVE",  (0,1), (-1,1), 0.5, colors.HexColor("#666")),
        ("LINEABOVE",  (0,-1), (-1,-1), 0.5, colors.HexColor("#666")),
        ("ALIGN",      (1,0), (1,-1), "RIGHT"),
    ]))
    story.append(summary_tbl)

    # #216 (Batch 7): present-tense narrative sits DIRECTLY UNDER the
    # aggregate table so numbers and words share a single frame — right
    # now, from `compute_counts`. Public wording is the same as B1; the
    # two reports must never disagree on what "right now" looks like.
    story.append(Spacer(0, 4))
    _now_lines = _current_state_narrative(current_counts, events)
    for _line in _now_lines:
        story.append(Paragraph(_html.escape(_line), body_style))

    # #268: what the numbers leave out, in one sentence. B2 is a strict
    # one-pager (#126) with almost no headroom, so this rides at footnote
    # size — small, but never absent: a bare figure on a public report is
    # exactly how a set-aside record gets read as a missing person.
    from people_counts import counts_notes_short as _counts_notes_b2
    story.append(Paragraph(
        _html.escape(_counts_notes_b2(current_counts)),
        PS("B2CountNote", parent=styles["Normal"], fontSize=7.5,
           leading=9, spaceBefore=1, spaceAfter=0,
           textColor=colors.HexColor("#555")),
    ))

    # D1 (Batch 7): explicit transition sentence between "right now" and
    # "during this window". A journalist or family member reading the
    # report must understand that the two sections count different
    # things before they scan the numbers below.
    story.append(Spacer(0, 3))
    story.append(Paragraph(
        "These count different things. The table above is the situation right now. "
        "The section below covers everything that happened during the period.",
        PS("B2SepNote", parent=body_style, fontSize=9.5, leading=12,
           textColor=colors.HexColor("#444444"), spaceAfter=2),
    ))

    # ── What happened during this window ────────────────────────────
    # Counts only — NO percentages on B2. A bare "68% rescued" would be
    # quoted by press as 68% of everyone caught in the earthquake, when
    # the denominator is only app users who checked in. Locked with
    # Paul 2026-08-12; see PRD "Legal / privacy locks".
    #
    # #216 (Batch 7): this section is PAST TENSE only. Present-tense
    # facts already sit next to the aggregate table above.
    #
    # The heading, chart AND its plain-language explanation are ONE
    # KeepTogether block (3b, 2026-08-13): a reader must never have to
    # turn the page to find out what the chart means.
    # #126 (Batch 7): the chart height came down from 50mm to 38mm and
    # the surrounding spacers were trimmed. The chart is still perfectly
    # legible at 38mm — the y-axis is 0..N (whole people), never a fine
    # decimal — and this is the single biggest saving on the page.
    # #268: 4pt -> 1pt. The timeline heading below already has spaceAfter,
    # so this spacer was pure padding, and #126 keeps B2 to one page —
    # tightening spacing is how the exclusions note earned its room.
    story.append(Spacer(0, 1))
    buckets, _hourly = _bucket_timeline(raw_rows, since_dt, until_dt)
    from reportlab.platypus import KeepTogether as _KT
    timeline_block = [Paragraph("What happened during this window", h2_style)]
    if any(b["trapped"] or b["safe"] or b["rescued"] for b in buckets):
        timeline_block.append(_timeline_chart(buckets, 170 * mm, 38 * mm))
        timeline_block.append(Spacer(0, 2))
        timeline_block.append(Paragraph(CHART_CAPTION, PS(
            "ChartCapPub", parent=styles["Normal"], fontSize=7.5, leading=10,
            textColor=colors.HexColor("#444444"))))
        timeline_block.append(Spacer(0, 3))
    timeline_block.extend(
        Paragraph(_html.escape(line), body_style)
        for line in _window_narrative(raw_rows, events, counts)
    )
    story.append(_KT(timeline_block))

    story.append(Spacer(0, 2))  # #268: 6pt -> 2pt; the footer has spaceBefore=8
    # D2 (Batch 7): neutral footer wording by default. The "conducted by
    # [authority]" claim is gated behind the same cooperation setting as
    # the issuer line.
    if _cooperation_ok:
        _notes_line = (
            "Notes: These counts reflect app users who have checked in via Quake Angel during the "
            "window shown. They do not represent the total affected population. Individual "
            "identities are not disclosed in this report to protect privacy and to preserve formal "
            f"next-of-kin notification procedures conducted by {authority}."
        )
    else:
        _notes_line = (
            "Notes: These counts reflect app users who have checked in via Quake Angel during the "
            "window shown. They do not represent the total affected population. Individual "
            "identities are not disclosed in this report to protect privacy and to preserve formal "
            "next-of-kin notification procedures conducted by the appropriate authorities."
        )
    story.append(Paragraph(_notes_line, footer_style))

    buf = _io.BytesIO()
    doc = r["SimpleDocTemplate"](
        buf,
        pagesize=r["A4"],   # PORTRAIT — printer-default friendly (1a, 2026-08-13)
        leftMargin=18*mm, rightMargin=18*mm,
        topMargin=15*mm, bottomMargin=15*mm,
        title="Quake Angel Public Status Report",
        author=principal.get("email", ""),
    )
    _onpage = _make_public_onpage(await _get_logo_image_reader())
    doc.build(story, onFirstPage=_onpage, onLaterPages=_onpage)
    return Response(
        content=buf.getvalue(),
        media_type="application/pdf",
        headers={
            # Plain-language filename (issue #133) — "public-report", not "B2".
            "Content-Disposition": f'attachment; filename="quakeangel-public-report-{datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")}.pdf"',
            # No X-Row-Count on B2 — the aggregate table IS the counts, and
            # exposing "N devices" in a header could be considered
            # identifiability-adjacent for very small N. Erring cautiously.
            "X-Report-Kind": "B2-public",
        },
    )






