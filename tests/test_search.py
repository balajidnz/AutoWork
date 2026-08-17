"""Row conversion for the JobSpy search source."""

from __future__ import annotations

import pytest

from autowork.sources import normalise_level
from autowork.sources.search import _clean, _iso


@pytest.mark.parametrize(
    "value,expected",
    [
        ("Software Engineer", "Software Engineer"),
        ("  padded  ", "padded"),
        # pandas leaves these where a field was absent; none may reach the DB
        ("nan", None), ("NaN", None), ("NaT", None), ("None", None),
        ("", None), (None, None), (float("nan"), None),
    ],
)
def test_clean_strips_pandas_placeholders(value, expected):
    assert _clean(value) == expected


@pytest.mark.parametrize(
    "value,expected",
    [
        ("2026-08-07", "2026-08-07T00:00:00+00:00"),
        ("2026-08-07 00:00:00", "2026-08-07T00:00:00+00:00"),
        ("NaT", None), ("nan", None), (None, None), ("", None),
    ],
)
def test_iso_handles_dates_and_missing(value, expected):
    assert _iso(value) == expected


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("entry level", "entry"),          # LinkedIn
        ("mid-senior level", "mid_senior"),
        ("associate", "associate"),
        ("mid_senior_level", "mid_senior"),  # SmartRecruiters
        ("entry_level", "entry"),
        # an absence of information, not a level
        ("not_applicable", None),
        (None, None),
    ],
)
def test_level_vocabularies_converge(raw, expected):
    assert normalise_level(raw) == expected
