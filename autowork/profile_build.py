"""Turn the setup wizard's answers into a working `profile/profiles.json`.

The ranking config has two halves. One describes the Indian entry-level
engineering market — which titles are not engineering, which level tokens mean
senior, which location strings are really abroad — and is the same for
everybody; that half lives in `profile_template.json` and is never edited by
hand. The other half is who you are and what you want, and is written here from
what the wizard collected.

Keeping them apart is what makes the tool usable by someone else: a new user
answers six questions instead of hand-tuning a 300-line config, and the gates
that took a corpus of 20,000 postings to calibrate come along for free.
"""

from __future__ import annotations

import json
import os
import re
from collections import Counter
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from autowork import db

TEMPLATE = Path(__file__).with_name("profile_template.json")
CONFIG = db.REPO_ROOT / "profile" / "profiles.json"

# Spellings a job board might use for each city, so "Bengaluru" also matches a
# posting that says "Bangalore" or "BLR".
CITY_TOKENS: dict[str, tuple[str, ...]] = {
    "bengaluru": ("bengaluru", "bangalore", "blr"),
    "hyderabad": ("hyderabad", "secunderabad", "hyd"),
    "pune": ("pune", "pimpri", "chinchwad"),
    "chennai": ("chennai", "madras"),
    "mumbai": ("mumbai", "bombay", "navi mumbai", "thane"),
    "delhi": ("delhi", "new delhi", "ncr"),
    "gurugram": ("gurugram", "gurgaon", "ncr"),
    "noida": ("noida", "ncr"),
    "kolkata": ("kolkata", "calcutta"),
    "ahmedabad": ("ahmedabad", "gandhinagar"),
    "kochi": ("kochi", "cochin", "ernakulam"),
    "coimbatore": ("coimbatore",),
    "jaipur": ("jaipur",),
    "indore": ("indore",),
    "chandigarh": ("chandigarh", "mohali"),
}

# Experience band -> (max years a posting may ask for, the stretch ceiling).
# The gap is deliberate: a posting asking for one year more than you have is
# worth applying to, two more is not, and without the stretch tier an
# entry-level search in India returns almost nothing.
EXPERIENCE_BANDS: dict[str, tuple[int, int, int]] = {
    "fresher": (1, 2, 1),
    "0-2": (2, 3, 2),
    "2-4": (4, 5, 3),
    "4-6": (6, 7, 4),
    "6+": (10, 12, 6),
}


def city_tokens(city: str) -> list[str]:
    """Match tokens for a home city, falling back to the name itself.

    Matched against the aliases as well as the canonical name. People type the
    spelling they use — "Bangalore", "Gurgaon", "Bombay" — and looking only at
    the canonical key gave them a single token, so a posting saying "Bengaluru"
    would not have matched their own city.
    """
    key = re.sub(r"[^a-z ]", "", (city or "").lower().split(",")[0]).strip()
    if not key:
        return []
    for name, tokens in CITY_TOKENS.items():
        if key == name or key in tokens:
            return list(tokens)
    # Then prefixes, for "Bengaluru Urban" and similar.
    for name, tokens in CITY_TOKENS.items():
        if key.startswith(name) or name.startswith(key):
            return list(tokens)
    return [key]


def weighted(items: list[str], text: str = "", top: int = 10,
             priority: list[str] | None = None) -> dict[str, int]:
    """Assign 5..10 weights, most-evidenced first, with chosen skills pinned.

    The ranker multiplies these, so a flat list would score a passing mention
    of Docker as highly as the thing someone actually builds with. Frequency is
    the only signal available from the document — but it measures what a resume
    *talks about*, not what its owner wants to be hired for. Measured on a real
    infrastructure resume: Redis 6 mentions, Kubernetes 2. Ranking on frequency
    alone would have targeted the wrong jobs, so the wizard asks which skills
    matter and those are pinned at 10 regardless of how often they appear.
    """
    if not items:
        return {}
    pinned = {p.lower() for p in (priority or [])}
    lowered = text.lower()
    # Word boundaries, not substrings: a plain `.count("go")` also matches
    # golang, google and "goes", which pushed Go above Kubernetes on an
    # infrastructure resume that barely mentions it.
    counts = Counter({
        item: len(re.findall(rf"(?<![a-z0-9]){re.escape(item.lower())}(?![a-z0-9])", lowered))
        for item in items
    }) if text else None
    ordered = (
        [item for item, _ in counts.most_common()] if counts
        else list(items)
    )
    weights: dict[str, int] = {}
    for index, item in enumerate(ordered):
        key = item.lower()
        if key in pinned:
            weights[key] = 10
            continue
        # Top `top` slide 9 -> 6; the tail sits at 5 so it still counts. Capped
        # below 10 so a pinned skill always outranks a merely frequent one.
        weights[key] = max(6, 9 - (index * 3 // max(top, 1))) if index < top else 5
    return weights


def build(answers: dict[str, Any]) -> dict:
    """Wizard answers -> a complete, valid config.

    Expected keys: name, email, city, remote_ok, home_only, experience_band,
    current_ctc_lpa, goal, and `resumes`: a list of
    {slug, label, path, roles, skills, text}.
    """
    config = json.loads(TEMPLATE.read_text(encoding="utf-8"))

    city = (answers.get("city") or "").strip()
    tokens = city_tokens(city)
    band = answers.get("experience_band") or "0-2"
    max_years, stretch_years, stretch_level = EXPERIENCE_BANDS.get(
        band, EXPERIENCE_BANDS["0-2"]
    )

    config["candidate"] = {
        "name": answers.get("name") or "",
        "email": answers.get("email") or "",
        "base": city,
        "experience_band": band,
        "experience_months_as_of": {
            "date": datetime.now(UTC).date().isoformat(),
            "months": int(round(float(answers.get("years") or 0) * 12)),
        },
        "current_ctc_lpa": answers.get("current_ctc_lpa"),
        "goal": answers.get("goal") or "maximise interview volume",
    }

    config["constraints"].update({
        "max_required_years": max_years,
        "stretch_max_years": stretch_years,
        "stretch_max_level": stretch_level,
        "home_city_name": city.split(",")[0].strip() or "your city",
        "home_city_tokens": tokens,
        "require_home_city": bool(answers.get("home_only")),
        # Which kinds of job count. The families you do not pick supply the
        # blocklist, so an engineer is not shown account-executive roles.
        "role_families": answers.get("families") or ["engineering"],
        "location_tiers": {
            "home_city": 12,
            # Remote outranks another Indian city when the user is open to it:
            # both are acceptable, but remote needs no relocation.
            "remote": 8 if answers.get("remote_ok", True) else 0,
            "other_india": 2,
        },
    })

    config["profiles"] = {}
    for index, resume in enumerate(answers.get("resumes") or []):
        slug = resume.get("slug") or f"profile{index + 1}"
        config["profiles"][slug] = {
            "label": resume.get("label") or slug.title(),
            # The extracted text, saved locally. Storing the uploaded
            # *filename* was the original behaviour and left tailoring broken
            # for anyone who set up through the wizard: the config pointed at
            # `cv.pdf` in the repo root, which nothing had ever written.
            "resume": _store_resume(slug, resume) or resume.get("path") or "",
            "preferred": index == 0,
            # Weights already tuned for a resume that was not re-uploaded are
            # kept as they are: re-deriving them from an empty text would rank
            # everything equal and quietly undo any hand-editing.
            "target_titles": (resume.get("title_weights")
                              or weighted(resume.get("roles") or [], resume.get("text", ""))),
            "skills": (resume.get("weights")
                       or weighted(resume.get("skills") or [], resume.get("text", ""),
                                   priority=resume.get("key_skills"))),
        }

    config["job_search"]["location"] = city or "India"
    return config


RESUME_DIR = db.REPO_ROOT / "profile"


def _store_resume(slug: str, resume: dict) -> str:
    """Save a resume's text as markdown, returning its repo-relative path.

    Tailoring needs the words, not the PDF — the prompt embeds the resume so a
    model can rewrite it. Nothing is written when the resume was not
    re-uploaded (editing an existing profile), so the file already on disk is
    left alone.
    """
    text = (resume.get("text") or "").strip()
    if not text:
        return ""
    RESUME_DIR.mkdir(parents=True, exist_ok=True)
    path = RESUME_DIR / f"resume-{re.sub(r'[^a-z0-9-]', '-', slug.lower())}.md"
    path.write_text(text, encoding="utf-8")
    # relpath, not Path.relative_to: the latter raises when RESUME_DIR sits
    # outside the repo, which is exactly how the tests redirect it.
    return os.path.relpath(path, db.REPO_ROOT)


def validate(config: dict) -> list[str]:
    """Problems that would make the config rank nothing. Empty list means fine."""
    problems: list[str] = []
    if not config.get("profiles"):
        problems.append("no resume added — there is nothing to match jobs against")
    for name, profile in (config.get("profiles") or {}).items():
        if not profile.get("target_titles"):
            problems.append(f"{name}: no target job titles")
        if not profile.get("skills"):
            problems.append(f"{name}: no skills")
    constraints = config.get("constraints", {})
    if constraints.get("require_home_city") and not constraints.get("home_city_tokens"):
        problems.append("set to home-city-only, but no city was given")
    if not (config.get("candidate") or {}).get("base"):
        problems.append("no city — every posting would be scored as a relocation")
    for name, profile in (config.get("profiles") or {}).items():
        path = profile.get("resume") or ""
        if path and not (db.REPO_ROOT / path).exists():
            problems.append(f"{name}: resume file {path} is missing")
    return problems


def save(config: dict, path: Path = CONFIG) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        # Never silently replace a config someone has tuned by hand. The wizard
        # only appears when there is no profile, but the endpoint behind it can
        # be reached again, and a year of adjustments should not vanish because
        # a page was reloaded.
        backup = path.with_name(f"{path.stem}.backup-{db.now()[:19].replace(':', '')}.json")
        backup.write_bytes(path.read_bytes())
    path.write_text(json.dumps(config, indent=2, ensure_ascii=False) + "\n",
                    encoding="utf-8")
    return path


def exists(path: Path = CONFIG) -> bool:
    return path.exists()


def to_answers(config: dict) -> dict:
    """The inverse of `build` — a saved config back into wizard answers.

    Needed so the profile can be edited rather than only created. The resume
    *text* is never stored (it is parsed in memory and discarded), so the
    weights already computed are handed back verbatim under `weights`; `build`
    uses those as-is unless the resume is re-uploaded. Without that, opening
    settings and pressing save would flatten a tuned profile.
    """
    constraints = config.get("constraints", {})
    candidate = config.get("candidate", {})
    tiers = constraints.get("location_tiers", {})
    months = (candidate.get("experience_months_as_of") or {}).get("months") or 0
    return {
        "name": candidate.get("name", ""),
        "email": candidate.get("email", ""),
        "city": candidate.get("base", ""),
        "years": round(months / 12, 1) if months else None,
        "experience_band": candidate.get("experience_band") or _band_for(months / 12),
        "current_ctc_lpa": candidate.get("current_ctc_lpa"),
        "remote_ok": bool(tiers.get("remote", 0)),
        "families": (constraints.get("role_families")
                     or ([constraints["role_family"]] if constraints.get("role_family")
                         else ["engineering"])),
        "home_only": bool(constraints.get("require_home_city",
                                          constraints.get("require_bangalore", False))),
        "resumes": [
            {
                "slug": slug,
                "label": profile.get("label") or slug.title(),
                "path": profile.get("resume") or "",
                "roles": list(profile.get("target_titles") or {}),
                "skills": list(profile.get("skills") or {}),
                "key_skills": [k for k, v in (profile.get("skills") or {}).items() if v >= 10],
                "weights": profile.get("skills") or {},
                "title_weights": profile.get("target_titles") or {},
                "text": "",
            }
            for slug, profile in (config.get("profiles") or {}).items()
        ],
    }


def _band_for(years: float) -> str:
    for band, (max_years, _, _) in EXPERIENCE_BANDS.items():
        if years < max_years:
            return band
    return "6+"


def families() -> list[dict]:
    """Role families the wizard can offer, from the shared template."""
    template = json.loads(TEMPLATE.read_text(encoding="utf-8"))
    return [{"key": key, "label": value.get("label") or key}
            for key, value in (template.get("role_families") or {}).items()]
