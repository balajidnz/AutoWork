"""Application tracking: what happens after you apply.

Sourcing and ranking end at the apply button. This is the other half — which
applications are live, which have gone quiet, and which are due a follow-up.

State lives in `data/status.json`, the same committed file the console already
writes, so nothing new needs persisting and the history survives the database
being rebuilt on every poll.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, date, datetime
from typing import Iterable

from . import db

# Ordered. `applied` onwards is a live application; the last two are closed.
PIPELINE = ("shortlisted", "applied", "screening", "interview", "offer", "rejected")
LIVE = ("applied", "screening", "interview")
CLOSED = ("rejected", "offer", "skipped")

# A week is long enough that silence is meaningful but short enough that the
# role is still open and the recruiter still recognises your name.
FOLLOW_UP_AFTER_DAYS = 7
# Past this a second nudge reads as pestering rather than diligence, and the
# requisition has usually moved on.
GIVE_UP_AFTER_DAYS = 30


@dataclass(slots=True)
class Application:
    job_id: str
    state: str
    updated_at: str
    note: str | None = None
    company: str = ""
    title: str = ""
    url: str = ""

    @property
    def days_since(self) -> int | None:
        try:
            when = datetime.fromisoformat(self.updated_at)
        except ValueError:
            return None
        if when.tzinfo is None:
            when = when.replace(tzinfo=UTC)
        return (datetime.now(UTC) - when).days

    @property
    def is_live(self) -> bool:
        return self.state in LIVE

    @property
    def needs_follow_up(self) -> bool:
        """Applied, gone quiet, and still inside the window where a nudge helps.

        Only `applied` qualifies. Once a conversation has started, silence is
        the recruiter's process rather than a dropped thread, and chasing it
        on a timer is the wrong instinct.
        """
        if self.state != "applied":
            return False
        days = self.days_since
        return days is not None and FOLLOW_UP_AFTER_DAYS <= days <= GIVE_UP_AFTER_DAYS

    @property
    def went_cold(self) -> bool:
        days = self.days_since
        return self.state == "applied" and days is not None and days > GIVE_UP_AFTER_DAYS


def load(conn) -> list[Application]:
    """Join the status ledger against whatever the corpus still knows.

    A posting can vanish from the boards after you apply — filled, pulled, or
    simply aged out of the poll — but the application still happened, so the
    ledger is the source of truth and job details are best-effort.
    """
    states = db.load_status()
    if not states:
        return []
    rows = {
        r["id"]: r
        for r in conn.execute(
            "SELECT id, company, title, url FROM jobs WHERE id IN (%s)"
            % ",".join("?" * len(states)),
            tuple(states),
        )
    }
    out = []
    for job_id, entry in states.items():
        row = rows.get(job_id)
        out.append(
            Application(
                job_id=job_id,
                state=entry.get("state", ""),
                updated_at=entry.get("updated_at", ""),
                note=entry.get("note"),
                company=row["company"] if row else "",
                title=row["title"] if row else "",
                url=row["url"] if row else "",
            )
        )
    return sorted(out, key=lambda a: a.updated_at, reverse=True)


def follow_ups(apps: Iterable[Application]) -> list[Application]:
    return sorted(
        (a for a in apps if a.needs_follow_up),
        key=lambda a: a.days_since or 0,
        reverse=True,
    )


def summary(apps: Iterable[Application]) -> dict[str, int]:
    apps = list(apps)
    counts = {state: 0 for state in PIPELINE}
    for app in apps:
        if app.state in counts:
            counts[app.state] += 1
    counts["live"] = sum(1 for a in apps if a.is_live)
    counts["follow_up"] = sum(1 for a in apps if a.needs_follow_up)
    counts["cold"] = sum(1 for a in apps if a.went_cold)
    return counts


def applied_this_week(apps: Iterable[Application]) -> int:
    # `a.days_since is not None`, not `a.days_since or N` — an application made
    # today has days_since == 0, which is falsy, so the `or` form silently
    # excluded exactly the applications it most needed to count.
    return sum(
        1 for a in apps
        if a.state in PIPELINE and a.state != "shortlisted"
        and a.days_since is not None and a.days_since <= 7
    )


def response_rate(apps: Iterable[Application]) -> float | None:
    """Share of applications that got past the initial silence.

    Only counts applications old enough to have plausibly been answered —
    including yesterday's in the denominator would drag the number down for no
    reason.
    """
    apps = [
        a for a in apps
        if a.state in ("applied", "screening", "interview", "offer", "rejected")
        and a.days_since is not None and a.days_since >= FOLLOW_UP_AFTER_DAYS
    ]
    if not apps:
        return None
    answered = sum(1 for a in apps if a.state != "applied")
    return answered / len(apps)


def next_states(state: str) -> tuple[str, ...]:
    """Plausible transitions, for the console's buttons."""
    return {
        "": ("shortlisted", "applied", "skipped"),
        "shortlisted": ("applied", "skipped"),
        "applied": ("screening", "rejected"),
        "screening": ("interview", "rejected"),
        "interview": ("offer", "rejected"),
        "offer": (),
        "rejected": (),
        "skipped": ("shortlisted",),
    }.get(state, PIPELINE)
