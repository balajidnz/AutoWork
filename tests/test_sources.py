"""Parsing tests for the source adapters.

These cover the field-shape quirks that differ per ATS: Greenhouse
entity-encodes its HTML, Lever dates are epoch milliseconds, and
SmartRecruiters needs its posting URL reconstructed.
"""

from __future__ import annotations

import pytest

from autowork import db
from autowork.sources import html_to_text, iso, looks_remote, parse_salary
from autowork.sources.smartrecruiters import posting_url


# --------------------------------------------------------------- html_to_text


def test_strips_tags_and_keeps_text():
    assert html_to_text("<p>Hello <b>world</b></p>") == "Hello world"


def test_unescapes_greenhouse_double_encoding():
    """Greenhouse returns HTML with its angle brackets entity-encoded, so the
    text arrives as `&lt;p&gt;About&lt;/p&gt;` and needs unescaping before the
    parser sees any markup at all."""
    assert html_to_text("&lt;h2&gt;About&lt;/h2&gt;&lt;p&gt;We build&lt;/p&gt;") == (
        "About\n\nWe build"
    )


def test_drops_script_and_style_bodies():
    out = html_to_text("<p>Real</p><script>var x=1;</script><style>a{}</style>")
    assert "var x" not in out and "a{}" not in out and "Real" in out


@pytest.mark.parametrize("value", [None, "", "   "])
def test_empty_html_is_none(value):
    assert html_to_text(value) is None


# ------------------------------------------------------------------------ iso


@pytest.mark.parametrize(
    "value,expected",
    [
        (1711403416463, "2024-03-25T21:50:16+00:00"),   # Lever: epoch millis
        ("2026-08-04T07:02:31-04:00", "2026-08-04T11:02:31+00:00"),  # Greenhouse
        ("2026-03-12T16:38:15.322+00:00", "2026-03-12T16:38:15+00:00"),  # Ashby
        ("2026-01-01T00:00:00Z", "2026-01-01T00:00:00+00:00"),
        ("not a date", None),
        (None, None),
        (0, None),
    ],
)
def test_iso_normalises_every_encoding(value, expected):
    assert iso(value) == expected


# --------------------------------------------------------------- looks_remote


@pytest.mark.parametrize(
    "values,expected",
    [
        (("Remote - India",), True),
        (("Work from home",), True),
        (("Bengaluru", "Anywhere"), True),
        (("Bengaluru",), False),
        ((None, ""), False),
    ],
)
def test_looks_remote(values, expected):
    assert looks_remote(*values) is expected


# --------------------------------------------------------------- parse_salary


@pytest.mark.parametrize(
    "text,low,high,ccy",
    [
        ("$257K – $335K • Offers Equity", 257_000, 335_000, "USD"),
        ("₹20L - ₹40L", 2_000_000, 4_000_000, "INR"),
        ("$150,000", 150_000, None, "USD"),
        # a bare small integer is years of experience, not money
        ("2 years experience", None, None, None),
        ("", None, None, None),
        (None, None, None, None),
    ],
)
def test_parse_salary(text, low, high, ccy):
    assert parse_salary(text) == (low, high, ccy)


# ----------------------------------------------------- smartrecruiters url


def test_posting_url_uses_identifier_casing_and_slug():
    """Both halves matter. The path takes the company's own casing, not the
    lowercase token we probed with, and must end in a title slug — without it
    the page loads and then its client-side router redirects in a loop."""
    built = posting_url(
        {"id": 6000000001295245, "name": "Software Dev Engineer I",
         "company": {"identifier": "SWIGGY"}},
        "swiggy",
    )
    assert built == (
        "https://jobs.smartrecruiters.com/SWIGGY/"
        "6000000001295245-software-dev-engineer-i"
    )


def test_posting_url_falls_back_to_probe_token():
    built = posting_url({"id": 123, "name": "Backend Engineer, Payments"}, "acme")
    assert built == "https://jobs.smartrecruiters.com/acme/123-backend-engineer-payments"


def test_posting_url_without_a_title():
    built = posting_url({"id": 123, "company": {"identifier": "ACME"}}, "acme")
    assert built == "https://jobs.smartrecruiters.com/ACME/123"


# ------------------------------------------------------------------ dedup


def test_company_slug_collapses_legal_suffixes():
    assert db.company_slug("Swiggy Technologies Pvt Ltd") == db.company_slug("Swiggy")
    assert db.company_slug("Acme Labs, Inc.") == db.company_slug("Acme")


def test_dedup_key_ignores_location():
    """The same opening is listed as "Bengaluru", "Bangalore, India" and
    "Remote - India" across boards; treating those as distinct defeats dedup."""
    a = db.Job(source="greenhouse", company="Acme", title="Backend Engineer",
               url="u", location="Bengaluru")
    b = db.Job(source="lever", company="Acme", title="Backend Engineer",
               url="u2", location="Remote - India")
    assert a.dedup_key == b.dedup_key
    assert a.id != b.id


def test_dedup_key_separates_different_roles():
    a = db.Job(source="greenhouse", company="Acme", title="Backend Engineer", url="u")
    b = db.Job(source="greenhouse", company="Acme", title="Frontend Engineer", url="u")
    assert a.dedup_key != b.dedup_key
