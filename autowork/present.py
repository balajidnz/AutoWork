"""Plain-language layer between the ranking pipeline and anything a person reads.

The ranker writes for itself — tiers, tracks, coverage ratios, "not yet on
aggregators", a score on a scale nobody states. That vocabulary is precise and
means nothing to a reader at 8am, so every internal value is translated here,
once, and the web page stays a dumb renderer of whatever this produces.

Kept separate from the server so it can be tested without binding a port.
"""

from __future__ import annotations

import json
import re
import sqlite3
from datetime import UTC, datetime

from autowork import comp as comp_mod
from autowork import coverage as cov
from autowork import db, rank, track

# Score bands. The raw number is meaningless without the distribution (the live
# corpus runs roughly 10..70), so the band is what gets shown and the number is
# demoted to a footnote.
MATCH_BANDS = (
    (60, "Strong match", "strong"),
    (45, "Good match", "good"),
    (30, "Worth a look", "fair"),
    (0, "Long shot", "weak"),
)

# Friendly names for the two profiles the original config shipped with. Any
# other profile uses its own configured label, so a user who set up with one
# resume called "main" is not told to send a blank.
RESUME_ALIASES = {
    "infra": ("DevOps-leaning", "profile/resume-infra.md"),
    "product": ("Full-stack", "profile/resume-product.md"),
}


def resume_map(config: dict) -> dict[str, tuple[str, str]]:
    """profile slug -> (what to call the resume, where it lives)."""
    out: dict[str, tuple[str, str]] = {}
    for slug, profile in (config.get("profiles") or {}).items():
        if slug in RESUME_ALIASES:
            out[slug] = RESUME_ALIASES[slug]
        else:
            out[slug] = (profile.get("label") or slug.title(), profile.get("resume") or "")
    return out

# Pipeline state -> button label. track.next_states() decides which are offered;
# this only decides how they read.
ACTIONS = {
    "shortlisted": "Save for later",
    "applied": "I applied",
    "skipped": "Not for me",
    "screening": "They replied",
    "interview": "Got an interview",
    "offer": "Got an offer",
    "rejected": "Rejected",
}

STATE_LABELS = {
    "shortlisted": "Saved",
    "applied": "Applied",
    "skipped": "Skipped",
    "screening": "In screening",
    "interview": "Interviewing",
    "offer": "Offer",
    "rejected": "Rejected",
}


def humanise(reason: str) -> tuple[str, str]:
    """Turn one ranking reason into (icon, sentence).

    Anything unrecognised falls through unchanged rather than being dropped, so
    a new reason in rank.py degrades to plain text instead of vanishing.
    """
    if match := re.fullmatch(r"title matches '(.+)'", reason):
        return "🎯", f"The title is one you're targeting — {match.group(1)}"
    if match := re.fullmatch(r"skills: (.+)", reason):
        return "🧰", f"Asks for skills you have — {match.group(1)}"
    if match := re.fullmatch(r"posted (\d+)d ago", reason):
        days = int(match.group(1))
        if days == 0:
            return "🕐", "Posted today"
        return "🕐", f"Posted {days} day{'s' if days > 1 else ''} ago"
    if match := re.fullmatch(r"asks only ([\d.]+)\+ years", reason):
        return "✅", f"Asks for only {match.group(1)}+ years of experience"

    return {
        "infra + product scope": ("🎛️", "Infrastructure work inside a product team — what you asked for"),
        "pure ops role — not the target": ("⚠️", "Leans pure operations rather than product engineering"),
        "LLM/agent work": ("🤖", "Involves LLM / AI-agent work"),
        "explicitly entry level": ("🌱", "Explicitly an entry-level role"),
        "no stated experience bar": ("✅", "No minimum experience stated"),
        "not yet on aggregators": ("💎", "Only on the company's own careers page — far fewer applicants"),
        "Bangalore": ("📍", "Based in Bangalore"),
        "remote": ("🏠", "Remote — no move needed"),
        "elsewhere in India": ("📍", "In India, but not Bangalore — would mean relocating"),
    }.get(reason, ("•", reason))


def match_band(score: float) -> tuple[str, str]:
    for floor, label, key in MATCH_BANDS:
        if score >= floor:
            return label, key
    return "Long shot", "weak"


def age_text(posted_at: str | None) -> str:
    if not posted_at:
        return ""
    try:
        days = (datetime.now(UTC) - datetime.fromisoformat(posted_at)).days
    except ValueError:
        return ""
    if days == 0:
        return "posted today"
    return f"posted {days} day{'s' if days > 1 else ''} ago"


def _applied_companies(rows: list[sqlite3.Row], states: dict) -> dict[str, str]:
    """Company slug -> title already applied to.

    Powers the duplicate-application warning: the two resumes surface the same
    employer on different tracks, and sending one recruiter two divergent CVs
    through a single ATS reads badly.
    """
    by_id = {r["id"]: r for r in rows}
    out: dict[str, str] = {}
    for job_id, state in states.items():
        if state.get("state") != "applied":
            continue
        if row := by_id.get(job_id):
            out[db.company_slug(row["company"])] = row["title"]
    return out


def build(conn: sqlite3.Connection) -> dict:
    """Everything the page needs, in one payload.

    `position` is the row's index in the *unfiltered* score-ordered shortlist —
    the same list `autowork show` indexes into. It must not be the position in
    whatever the reader has filtered down to, or `/tailor 3` tailors a
    different job than the one row 3 is showing.
    """
    rows = rank.shortlist(conn, 10_000, tier=None)
    states = db.load_status()
    applied = _applied_companies(rows, states)
    config = rank.load_config()
    owned = cov.candidate_terms(config)
    comp_cache = comp_mod.load()
    floor = config["candidate"].get("current_ctc_lpa")
    # Whose home city, in their words — the page labels its filter with this,
    # so a user in Pune does not get a "Bangalore only" toggle.
    city = rank.home_city(config["constraints"])
    tokens = rank.home_tokens(config["constraints"])
    home_re = re.compile(rf"(?<![a-z])({'|'.join(tokens)})(?![a-z])", re.I) if tokens else None
    resumes = resume_map(config)

    jobs = []
    for position, row in enumerate(rows, start=1):
        state = (states.get(row["id"]) or {}).get("state") or ""
        label, band = match_band(row["score"])
        resume, resume_path = resumes.get(row["profile"], ("your resume", ""))

        try:
            reasons = json.loads(row["reasons"] or "[]")
        except json.JSONDecodeError:
            reasons = []

        gaps = cov.analyse(row["description"], owned)
        entry = comp_mod.lookup(comp_cache, row["company"], row["title"])
        clash = applied.get(db.company_slug(row["company"]))

        pay = None
        if entry and entry.found:
            below = bool(floor and entry.median and entry.median < floor)
            pay = {"text": entry.summary(floor), "below": below}

        jobs.append({
            "id": row["id"],
            "position": position,
            "title": row["title"],
            "company": row["company"],
            "location": row["location"] or "Location not stated",
            "url": row["url"],
            "age": age_text(row["posted_at"]),
            "posted_at": row["posted_at"] or "",
            "score": round(row["score"], 1),
            "band": band,
            "bandLabel": label,
            "tier": row["tier"],
            "home": bool(home_re and home_re.search(row["location"] or "")),
            "family": rank.family_of(row["title"], config),
            "resume": resume,
            "resumePath": resume_path,
            # Freshness is dropped: the row header already shows the age, and
            # this reason was frozen when the job was ranked, so a posting
            # ranked yesterday shows "Posted 1 day ago" beside a header saying
            # two — the same fact, disagreeing with itself.
            "reasons": [{"icon": i, "text": t}
                        for i, t in map(humanise, reasons) if i != "🕐"],
            "coverage": {
                "have": len(gaps.have),
                "total": gaps.total,
                "ratio": round(gaps.ratio, 3),
                "missing": gaps.missing[:8],
            },
            "pay": pay,
            "state": state,
            "stateLabel": STATE_LABELS.get(state, ""),
            "actions": [{"state": s, "label": ACTIONS.get(s, s.title())}
                        for s in track.next_states(state)],
            # Only the clash, never the current role's own title.
            "clash": clash if clash and clash != row["title"] else None,
            "description": (row["description"] or "")[:8000],
        })

    apps = track.load(conn)
    counts = track.summary(apps)
    rate = track.response_rate(apps)
    return {
        "jobs": jobs,
        "meta": {
            "postings": conn.execute("SELECT COUNT(*) n FROM jobs").fetchone()["n"],
            "boards": len(db.verified_boards(conn)),
            "homeCity": city,
            "families": [
                {"key": name, "label": (config["role_families"][name].get("label") or name)}
                for name in rank.selected_families(config)
            ],
            "applied": counts["live"],
            "thisWeek": track.applied_this_week(apps),
            "responseRate": None if rate is None else round(rate, 3),
            "followUps": [
                {"company": a.company, "days": a.days_since} for a in track.follow_ups(apps)
            ],
        },
    }
