"""Application pipeline: follow-up timing, counts, and state transitions."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from autowork import track


def app(state: str = "applied", days_ago: int = 0, **over) -> track.Application:
    stamp = (datetime.now(UTC) - timedelta(days=days_ago)).isoformat(timespec="seconds")
    return track.Application(
        job_id=f"src:{state}-{days_ago}", state=state, updated_at=stamp,
        company="Acme", title="Backend Engineer", **over,
    )


# ------------------------------------------------------------- follow-ups


@pytest.mark.parametrize(
    "days,expected",
    [
        (0, False),    # applied today
        (6, False),    # still inside the quiet period
        (7, True),     # window opens
        (14, True),
        (30, True),    # last day worth nudging
        (31, False),   # past it — this is cold, not due
    ],
)
def test_follow_up_window(days, expected):
    assert app(days_ago=days).needs_follow_up is expected


@pytest.mark.parametrize("state", ["screening", "interview", "offer", "rejected", "shortlisted"])
def test_only_silent_applications_need_chasing(state):
    """Once a conversation has started, silence is the recruiter's process —
    chasing it on a timer is the wrong instinct."""
    assert app(state=state, days_ago=14).needs_follow_up is False


def test_cold_is_distinct_from_due():
    cold = app(days_ago=45)
    assert cold.went_cold is True
    assert cold.needs_follow_up is False


def test_follow_ups_are_ordered_oldest_first():
    apps = [app(days_ago=8), app(days_ago=20), app(days_ago=12)]
    assert [a.days_since for a in track.follow_ups(apps)] == [20, 12, 8]


# ----------------------------------------------------------------- counts


def test_applied_today_counts_this_week():
    """days_since == 0 is falsy; an `or` default silently dropped exactly the
    applications most worth counting."""
    assert track.applied_this_week([app(days_ago=0)]) == 1


def test_applied_this_week_excludes_older_and_shortlisted():
    apps = [app(days_ago=0), app(days_ago=3), app(days_ago=30),
            app(state="shortlisted", days_ago=1)]
    assert track.applied_this_week(apps) == 2


def test_summary_counts_states_and_derived_buckets():
    apps = [app(days_ago=10), app(state="screening", days_ago=2),
            app(state="rejected", days_ago=5), app(days_ago=60)]
    counts = track.summary(apps)
    assert counts["applied"] == 2
    assert counts["screening"] == 1
    assert counts["rejected"] == 1
    assert counts["live"] == 3          # applied + screening
    assert counts["follow_up"] == 1     # only the 10-day-old one
    assert counts["cold"] == 1          # the 60-day-old one


# ---------------------------------------------------------- response rate


def test_response_rate_ignores_applications_too_recent_to_judge():
    """Counting yesterday's application as 'no response' drags the number down
    for no reason."""
    assert track.response_rate([app(days_ago=1), app(days_ago=2)]) is None


def test_response_rate_counts_anything_past_applied():
    apps = [app(days_ago=10), app(state="screening", days_ago=10),
            app(state="rejected", days_ago=10), app(state="interview", days_ago=10)]
    assert track.response_rate(apps) == pytest.approx(0.75)


def test_response_rate_all_silent():
    assert track.response_rate([app(days_ago=20), app(days_ago=20)]) == 0.0


# ------------------------------------------------------------ transitions


@pytest.mark.parametrize(
    "state,expected",
    [
        ("", ("shortlisted", "applied", "skipped")),
        ("applied", ("screening", "rejected")),
        ("screening", ("interview", "rejected")),
        ("interview", ("offer", "rejected")),
        ("offer", ()),
        ("rejected", ()),
    ],
)
def test_next_states(state, expected):
    assert track.next_states(state) == expected


def test_malformed_timestamp_does_not_crash():
    broken = track.Application(job_id="x", state="applied", updated_at="not-a-date")
    assert broken.days_since is None
    assert broken.needs_follow_up is False
    assert broken.went_cold is False
