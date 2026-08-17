"""Regression tests for the gating logic.

Every case marked ``# bug:`` is one that actually shipped and was caught by
reading output rather than by a test. The gates decide what reaches the digest,
so a regression here does not raise — it silently hides real jobs or floods the
list with roles that were meant to be filtered.
"""

from __future__ import annotations

import pytest

from autowork import rank


@pytest.fixture(scope="module")
def cfg() -> dict:
    return rank.load_config()


def job(**over) -> dict:
    """A posting that passes every gate, so a test changes exactly one thing."""
    return {
        "title": "Software Engineer",
        "location": "Bengaluru, India",
        "remote": 0,
        "description": "",
        "posted_at": None,
        **over,
    }


# ------------------------------------------------------------ title_level


@pytest.mark.parametrize(
    "title,expected",
    [
        # levels that must be detected
        ("Software Engineer 3", 3),
        ("Backend Engineer II", 2),
        ("Data Engineer IV", 4),
        ("SDE-2", 2),
        ("SDE II", 2),
        ("SDE III Gen AI", 3),          # bug: tail-anchored regex missed mid-title
        ("Associate TSE II", 2),        # bug: no role noun to anchor to
        ("Analyst III", 3),
        ("L4 Engineer", 3),             # bug: only the tail was inspected
        ("Software Engineer 3 - Payments", 3),
        # entry level, or no level at all
        ("Software Engineer I", 1),
        ("SDE 1", 1),
        ("Full Stack Engineer 1", 1),
        ("L2 Engineer", 1),
        ("Platform Engineer", None),
        ("Associate Software Engineer", None),
        # version numbers that are not levels
        ("Engineer, Vue 3", None),      # bug: read as level 3
        ("Web3 Engineer", None),
        ("Go 1.22 Engineer", None),
        ("Analytics Engineer - Finance", None),
    ],
)
def test_title_level(title, expected):
    assert rank.title_level(title) == expected


# --------------------------------------------------------- required_years


@pytest.mark.parametrize(
    "text,expected",
    [
        ("We need 3+ years of experience in Go.", 3),
        ("1-3 years of professional experience required.", 1),
        ("At least 18 months of experience with Rails.", 1.5),
        # the lowest stated bar wins — deliberately lenient
        ("Minimum of 5 years experience; 2+ years with Kubernetes.", 2),
        # company boilerplate is not an experience requirement
        ("Founded 10 years ago, we now serve millions.", None),
        ("No stated requirement here.", None),
        ("", None),
        (None, None),
    ],
)
def test_required_years(text, expected):
    assert rank.required_years(text) == expected


# ------------------------------------------------------------- location


@pytest.mark.parametrize(
    "location,remote,ok",
    [
        # India
        ("Bengaluru, India", 0, True),
        ("India", 0, True),
        ("Remote - India", 1, True),
        ("Bengaluru; Gurugram", 0, True),
        ("Pune, Maharashtra", 0, True),
        ("Remote, Bangalore", 1, True),
        ("APAC", 0, True),
        # ISO codes: aggregators and SmartRecruiters emit "KA, IN" not "Bengaluru"
        ("KA, IN", 0, True),
        ("MH, IN", 0, True),
        ("Bengaluru, KA, IN", 0, True),
        # "IN" is also Indiana's state code — must not match
        ("Indianapolis, IN", 0, False),
        ("Fort Wayne, IN", 0, False),
        # substring false positives
        ("Indiana, USA", 0, False),      # bug: "india" matched inside "Indiana"
        ("Indianapolis, IN", 0, False),  # bug: same
        ("Indianola", 0, False),
        ("Malaysia", 0, False),          # bug: "asia" matched inside "Malaysia"
        ("Kuala Lumpur, Malaysia", 0, False),
        # remote, scoped elsewhere — these leaked while the gate used a
        # blacklist of foreign place names, which can never be complete
        ("Remote - US", 1, False),
        ("Sweden (Remote)", 1, False),
        ("Remote (Buenos Aires, Argentina)", 1, False),
        ("Remote, United Arab Emirates", 1, False),
        ("Munich", 0, False),
        ("Middle East", 0, False),
        # genuinely unscoped remote is fine
        ("Remote", 1, True),
        ("Anywhere", 0, True),
        (None, 1, True),
        # on-site somewhere unstated is unknown, not remote
        ("Hybrid", 1, False),            # bug: trusted the source's remote flag
        ("In-Office", 1, False),
        ("SF Office", 1, False),
        (None, 0, False),
    ],
)
def test_location_ok(cfg, location, remote, ok):
    assert rank.location_ok(location, bool(remote), cfg)[0] is ok


@pytest.mark.parametrize(
    "location,remote,label",
    [
        ("Bengaluru, India", 0, "Bangalore"),
        ("Bangalore", 0, "Bangalore"),
        ("Remote", 1, "remote"),
        ("Pune", 0, "elsewhere in India"),
    ],
)
def test_location_tier_prefers_bangalore_then_remote(cfg, location, remote, label):
    points, got = rank.location_tier(location, bool(remote), cfg)
    assert got == label
    # remote must outrank another Indian city: both are acceptable, only one
    # requires relocating.
    if label == "remote":
        assert points > rank.location_tier("Pune", False, cfg)[0]


# ------------------------------------------------------------ apply_gates


@pytest.mark.parametrize(
    "title,rejected",
    [
        ("Senior Software Engineer", True),
        ("Staff Engineer", True),
        ("Engineering Manager", True),
        ("Principal Data Platform Engineer", True),
        ("Tech Lead Manager", True),
        ("Sr. AI Enablement Engineer", True),
        # not engineering seats
        ("Account Executive, Enterprise", True),
        ("Office Operations Associate", True),   # bug: scored 41 on keyword overlap
        ("Fraud Operations Associate", True),
        ("Technical Support Engineer", True),
        ("Sales Engineer, SMB", True),
        ("Software Engineering Intern", True),
        # substring over-blocks
        ("Software Engineer, Internal Tools", False),   # bug: "intern" matched
        ("International Payments Engineer", False),     # bug: same
        # keepers
        ("Platform Engineer", False),
        ("Backend Engineer", False),
        ("Member of Technical Staff", False),
        # exemption must not disarm the real modifiers
        ("Senior Member of Technical Staff", True),
        ("Member of Technical Staff, Manager", True),
    ],
)
def test_role_and_seniority_gates(cfg, title, rejected):
    assert bool(rank.apply_gates(job(title=title), cfg)) is rejected


def test_stale_postings_are_gated(cfg):
    fresh = job(posted_at="2099-01-01T00:00:00+00:00")
    ancient = job(posted_at="2020-01-01T00:00:00+00:00")
    assert rank.apply_gates(fresh, cfg) is None
    assert "stale" in rank.apply_gates(ancient, cfg)


# ------------------------------------------------------------ stretch tier


def test_level_two_is_stretch_not_rejected(cfg):
    gate, tier = rank.gate_with_tier(job(title="SDE II"), cfg)
    assert gate is None and tier == "stretch"


def test_level_three_is_still_rejected(cfg):
    gate, _ = rank.gate_with_tier(job(title="SDE III"), cfg)
    assert gate is not None


def test_three_year_ask_is_stretch(cfg):
    gate, tier = rank.gate_with_tier(job(description="3+ years of experience"), cfg)
    assert gate is None and tier == "stretch"


def test_five_year_ask_is_rejected(cfg):
    gate, _ = rank.gate_with_tier(job(description="5+ years of experience"), cfg)
    assert gate is not None and "experience" in gate


def test_clean_posting_is_core(cfg):
    gate, tier = rank.gate_with_tier(job(), cfg)
    assert gate is None and tier == "core"
