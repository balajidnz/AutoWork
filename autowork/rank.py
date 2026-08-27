"""Score postings against the candidate profiles.

Two stages. **Gates** are hard constraints — wrong seniority, wrong country —
and a posting that fails one never reaches the digest no matter how well it
scores. **Signals** are additive and produce the ordering within what survives.

The bias throughout is recall: the stated goal is interview volume, so
ambiguous cases are admitted with a note rather than dropped silently.
"""

from __future__ import annotations

import json
import math
import re
import sqlite3
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

from . import db

PROFILES_PATH = db.REPO_ROOT / "profile" / "profiles.json"


def load_config(path: Path | None = None) -> dict:
    """Read the user's profile.

    The default resolves at call time, not at import: `PROFILES_PATH` is a
    module attribute the tests redirect, and a default argument would have been
    bound once when the module first loaded and ignored every redirect.
    """
    return json.loads((path or PROFILES_PATH).read_text(encoding="utf-8"))


# ---------------------------------------------------------------- seniority

# A trailing level only counts when it directly follows a role noun. Without
# that anchor "Engineer, Vue 3" and "Analytics Engineer - Finance 2" read as
# level 3 and level 2, and get wrongly rejected.
_ROLE_NOUN = (
    r"(?:engineer|developer|sde|swe|programmer|scientist|analyst"
    r"|specialist|consultant|architect|designer)"
)
# No end anchor: "SDE III Gen AI" and "Software Engineer 3 - Payments" carry
# their level mid-title, and anchoring to the tail let both through.
_LEVEL_TAIL = re.compile(
    rf"\b{_ROLE_NOUN}\b[\s\-–,/]+((?:i{{1,3}}|iv|v)|[1-6]|l[1-6]|e[1-6])\b", re.I
)
_LEVEL_LEAD = re.compile(r"^\s*(l[1-6]|e[1-6])\b", re.I)
# A trailing roman numeral of II or higher is a level regardless of what
# precedes it — "Associate TSE II" has no recognised role noun to anchor to.
# Bare trailing digits are excluded here: "Engineer, Vue 3" is not a level.
_ROMAN_TAIL = re.compile(r"\b(ii|iii|iv|v)\s*$", re.I)
_SDE_LEVEL = re.compile(r"\b(?:sde|swe|ic)[\s\-_]?(i{1,3}|iv|v|[1-6])\b", re.I)
_ROMAN = {"i": 1, "ii": 2, "iii": 3, "iv": 4, "v": 5}


def _band(token: str) -> int | None:
    token = token.lower()
    if token in _ROMAN:
        return _ROMAN[token]
    if token.isdigit():
        return int(token)
    if token[0] in "le" and token[1:].isdigit():
        # Ladder bands differ by company, but L1–L3 / E1–E3 are entry-to-junior
        # almost everywhere and L4+ / E4+ is mid or above.
        return 1 if int(token[1:]) <= 3 else 3
    return None


def title_level(title: str) -> int | None:
    """Numeric seniority band encoded in a job title, if any."""
    if m := _SDE_LEVEL.search(title):
        return _band(m.group(1))
    if m := _LEVEL_TAIL.search(title.strip()):
        return _band(m.group(1))
    if m := _LEVEL_LEAD.search(title.strip()):
        return _band(m.group(1))
    if m := _ROMAN_TAIL.search(title.strip()):
        return _band(m.group(1))
    return None


# "3+ years", "2-4 years", "minimum of 5 years", "at least 18 months"
_YEARS = re.compile(
    r"(\d{1,2})\s*(?:\+|plus)?\s*(?:-|–|—|to)?\s*(\d{1,2})?\s*\+?\s*(?:\+\s*)?(years?|yrs?)\b",
    re.I,
)
_MONTHS = re.compile(r"(\d{1,2})\s*\+?\s*months?\b", re.I)

# What a real requirement sits next to. Deliberately wider than "experience":
# "5+ years in SRE, production operations" and "5+ years in ML systems" are
# both genuine bars carrying none of the original keywords, and both were
# observed passing the gate on a live corpus.
_EXP_CONTEXT = re.compile(
    r"experien|background|track record|professional|"
    r"years?\s+(?:in|of|with|as|building|developing|designing|leading|writing)|"
    r"minimum|at least|require|qualif|must have|you have|looking for|seeking",
    re.I,
)

# A number of years next to something that is not a requirement at all. An
# allowlist alone cannot separate these, because "Generous equity grant vested
# over 4 years" sits inches from the word "experience" in a benefits list.
_NOT_EXPERIENCE = re.compile(
    r"vest|equity|stock|option grant|founded|anniversar|heritage|"
    r"years later|years ago|family company|in business|warrant|lease|"
    r"pto|paid time off|holiday|parental|maternity|notice period",
    re.I,
)

# "Software Engineer (5-7 years)", "SDE - 4+ yrs". The most explicit statement
# a posting makes about seniority, and it was being ignored entirely.
_TITLE_YEARS = re.compile(r"(\d{1,2})\s*(?:\+|-|–|to)?\s*(\d{1,2})?\s*\+?\s*(?:years?|yrs?)", re.I)


# Greenhouse and Lever emit markdown-escaped punctuation, so "2-5 years"
# arrives as "2\\-5 years". The range regex cannot cross the backslash: it
# skipped the 2, matched "5 years", and read the top of the range as the bar —
# turning a role squarely in the stretch band into a five-year one.
_MD_ESCAPE = re.compile(r"\\([-.+()\[\]])")


def _mentions(text: str) -> list[tuple[int, float]]:
    """(position, years) for every plausible experience bar, in document order."""
    text = _MD_ESCAPE.sub(r"\1", text)
    out: list[tuple[int, float]] = []
    for match in _YEARS.finditer(text):
        window = text[max(0, match.start() - 90) : match.end() + 90]
        if _NOT_EXPERIENCE.search(window) or not _EXP_CONTEXT.search(window):
            continue
        value = int(match.group(1))
        if 0 < value <= 20:
            out.append((match.start(), float(value)))
    for match in _MONTHS.finditer(text):
        window = text[max(0, match.start() - 90) : match.end() + 90]
        if _NOT_EXPERIENCE.search(window) or not _EXP_CONTEXT.search(window):
            continue
        months = int(match.group(1))
        if 0 < months <= 120:
            out.append((match.start(), months / 12))
    out.sort()
    return out


def required_years(text: str | None, title: str | None = None) -> float | None:
    """The experience bar this posting states, in years.

    Two rules, both learned from postings that got through:

    **The title wins.** "Software Engineer (5-7 years)" is the most explicit
    thing a posting says about seniority, and reading only the description
    missed it completely.

    **Otherwise take the first mention, not the smallest.** Postings state a
    general bar and then narrower ones — "8+ years of software development
    experience and 3+ years in leading teams" — and the general bar comes
    first. Taking the minimum read that as a three-year role and ranked it
    highly for someone with eighteen months.
    """
    if title and (match := _TITLE_YEARS.search(title)):
        value = int(match.group(1))
        if 0 < value <= 20:
            return float(value)
    if not text:
        return None
    found = _mentions(text)
    return found[0][1] if found else None


# ----------------------------------------------------------------- location


# Work-arrangement words and punctuation. Whatever survives stripping these is
# a geographic qualifier.
_LOC_NOISE = re.compile(
    r"\b(remote|hybrid|on-?site|in-?office|work from home|wfh|anywhere|flexible"
    r"|multiple locations|various|global|worldwide|any location|distributed)\b"
    r"|[()\[\]{},;/|·•\-–—]",
    re.I,
)


_ONSITE = re.compile(r"\b(hybrid|in-?office|on-?site)\b", re.I)

# Built once from the config. Word-boundary matched because plain substring
# matching makes "Indiana, USA" an India location and "Malaysia" an Asia one —
# both observed in real RemoteOK postings.
_INDIA_RE: re.Pattern | None = None


# Aggregators and SmartRecruiters emit ISO codes rather than names — Indeed
# returns "KA, IN" for Bengaluru. `IN` alone cannot be trusted: it is India's
# country code *and* Indiana's US state code, so "Indianapolis, IN" would match.
# Requiring a recognised Indian state code immediately before it disambiguates.
_IN_STATE_CODES = (
    "ka|mh|tn|tg|ts|dl|up|hr|wb|gj|rj|kl|ap|pb|mp|br|jh|as|ch|ga|od|or|ut|uk"
)
_ISO_INDIA = re.compile(rf"(?<![a-z])({_IN_STATE_CODES})\s*,\s*in(?![a-z])", re.I)


def _india_re(cfg: dict) -> re.Pattern:
    global _INDIA_RE
    if _INDIA_RE is None:
        joined = "|".join(re.escape(t) for t in cfg["constraints"]["india_tokens"])
        # The alternation MUST be grouped. Without the parentheses the pattern
        # reads as `((?<![a-z])india) | (bengaluru) | ... | (asia(?![a-z]))`,
        # so the lookarounds guard only the first and last branches and
        # "Indiana" still matches on the bare `india` alternative.
        _INDIA_RE = re.compile(rf"(?<![a-z])({joined})(?![a-z])", re.I)
    return _INDIA_RE


def location_ok(location: str | None, remote: bool, cfg: dict) -> tuple[bool, str]:
    """India-eligible?

    Deliberately allowlist-shaped. An earlier version blacklisted foreign place
    names, which silently admitted every country not on the list — Sweden,
    Munich and Buenos Aires all sailed through. Instead: require positive India
    evidence, or a remote posting with no geographic qualifier at all.
    """
    c = cfg["constraints"]
    text = (location or "").lower()

    if _india_re(cfg).search(text) or _ISO_INDIA.search(text):
        return True, "india"

    says_remote = "remote" in text or "anywhere" in text
    # "Hybrid" and "In-Office" mean there is a specific office to show up at.
    # The board just is not saying which, so this is unknown, not remote —
    # trusting the source's remote flag here admitted a Madrid-based role.
    if _ONSITE.search(text) and not says_remote:
        return False, f"on-site at an unstated location: {location}"

    residue = " ".join(_LOC_NOISE.sub(" ", text).split())
    if says_remote or remote:
        if residue:
            return False, f"remote but scoped to '{residue}'"
        return True, "remote (unscoped)"
    if not text:
        return False, "no location given"
    return False, f"outside India: {location}"


def home_tokens(c: dict) -> list[str]:
    """Spellings of the user's home city.

    `bangalore_tokens` was the original name, from when this ran for one person
    in one city. Both names work: a friend in Pune should not have their home
    city stored under a key naming someone else's.
    """
    return c.get("home_city_tokens") or c.get("bangalore_tokens") or []


def home_city(c: dict) -> str:
    """Display name for the home city — reaches the reader in the digest."""
    return c.get("home_city_name") or "Bangalore"


def requires_home_city(c: dict) -> bool:
    return bool(c.get("require_home_city", c.get("require_bangalore", False)))


def location_tier(location: str | None, remote: bool, cfg: dict) -> tuple[float, str]:
    """Rank eligible locations by how much upheaval they imply.

    Bangalore is home, remote needs no move, another Indian city does.
    """
    c = cfg["constraints"]
    tiers = c["location_tiers"]
    text = (location or "").lower()

    if re.search(rf"(?<![a-z])({'|'.join(home_tokens(c))})(?![a-z])", text, re.I):
        return float(tiers.get("home_city", tiers.get("bangalore"))), home_city(c)
    if remote or "remote" in text or "anywhere" in text:
        return float(tiers["remote"]), "remote"
    return float(tiers["other_india"]), "elsewhere in India"


# -------------------------------------------------------------------- gates


# The title must name a seat in the field the user actually works in. Kept
# broad enough for titles that skip the obvious noun entirely (SDE, Member of
# Technical Staff). Used when a config predates `role_families`.
_ENGINEERING_TITLE = re.compile(
    r"\b(engineer|engineering|developer|programmer|sde|swe"
    r"|software development|technical staff|devops|sre)\b",
    re.I,
)


def selected_families(cfg: dict) -> list[str]:
    """Names of the role families in play. Accepts the old single-value key."""
    c = cfg.get("constraints", {})
    chosen = c.get("role_families")
    if isinstance(chosen, str):
        chosen = [chosen]
    if not chosen:
        single = c.get("role_family")
        chosen = [single] if single else []
    available = cfg.get("role_families") or {}
    return [name for name in chosen if name in available]


def role_families(cfg: dict) -> list[dict]:
    available = cfg.get("role_families") or {}
    return [available[name] for name in selected_families(cfg)]


def role_family(cfg: dict) -> dict:
    """The primary family, for labelling. {} on a pre-families config."""
    families = role_families(cfg)
    return families[0] if families else {}


def family_title_re(cfg: dict) -> re.Pattern | None:
    """Allowlist for "is this even the right kind of job".

    The union of every family selected, so choosing engineering and data admits
    both. None means no allowlist — the `any` family, for fields the shipped
    list does not name. Filtering then rests on target titles and skills alone,
    which is looser, but returning nothing at all is not a better answer.
    """
    families = role_families(cfg)
    if not families:
        return _ENGINEERING_TITLE
    patterns = [(f.get("title_pattern") or "").strip() for f in families]
    if any(not p for p in patterns):
        return None                       # `any` is selected; it wins outright
    return re.compile(rf"\b({'|'.join(patterns)})\b", re.I)


def family_blocks(cfg: dict) -> list[str]:
    """Titles that pass the allowlist but are the wrong job anyway.

    Each family blocks its neighbours, so the families you did *not* choose end
    up on the blocklist — "Support Engineer" contains "engineer", "Design
    Engineer" contains "design", and the allowlist is deliberately too coarse
    to tell.

    Across several families this is the **intersection**, not the union: a term
    is only wrong if every family you picked says so. Union would have someone
    open to engineering and data blocking "data engineer" — a job squarely
    inside both.
    """
    families = role_families(cfg)
    if not families:
        return cfg.get("constraints", {}).get("block_role_terms", [])
    common = set(families[0].get("block_role_terms") or [])
    for family in families[1:]:
        common &= set(family.get("block_role_terms") or [])
    # First family's ordering, so the config stays readable and diffable.
    return [t for t in (families[0].get("block_role_terms") or []) if t in common]


def family_of(title: str, cfg: dict) -> str | None:
    """Which selected family a title belongs to — for filtering in the console."""
    available = cfg.get("role_families") or {}
    for name in selected_families(cfg):
        pattern = (available[name].get("title_pattern") or "").strip()
        if pattern and re.search(rf"\b({pattern})\b", title or "", re.I):
            return name
    return None


@dataclass(slots=True)
class Score:
    job_id: str
    profile: str
    score: float = 0.0
    passed: bool = True
    tier: str = "core"
    gate: str | None = None
    reasons: list[str] = field(default_factory=list)


def apply_gates(job: sqlite3.Row, cfg: dict) -> str | None:
    """Return a rejection reason, or None if the posting is eligible."""
    reason, _ = gate_with_tier(job, cfg)
    return reason


def gate_with_tier(job: sqlite3.Row, cfg: dict,
                   delisted: set[str] | None = None) -> tuple[str | None, str]:
    """Gate a posting and say which tier it lands in.

    Seniority is not binary for a 21-month candidate. "Staff Engineer" is out
    of reach; "SDE II" or a 3-year ask is an ordinary application. Both fail a
    single strict gate, so the near-misses are kept as `stretch` and surfaced
    separately rather than dropped or blended into the main list.
    """
    c = cfg["constraints"]
    title = (job["title"] or "").lower()
    tier = "core"

    # Taken down since the last poll. Checked first: everything below is about
    # whether the role suits you, and none of it matters for a dead link.
    if delisted and job["id"] in delisted:
        return "delisted: no longer on the board", tier

    # Allowlist, for the same reason the location gate is one: enumerating
    # non-engineering titles never converges. "Office Operations Associate"
    # and "Fraud Operations Associate" both scored well on incidental keyword
    # overlap until the title had to name an engineering role.
    allowlist = family_title_re(cfg)
    if allowlist and not allowlist.search(title):
        label = (role_family(cfg).get("label") or "engineering").lower()
        return f"role: title is not {label}", tier

    # Word-boundary matched, not substring: a plain `"intern" in title` check
    # also rejects "Internal Tools" and "International Payments".
    for term in family_blocks(cfg):
        if re.search(rf"(?<![a-z]){re.escape(term.strip())}(?![a-z])", title):
            return f"role: not an engineering seat ('{term.strip()}')", tier

    # senior / staff / manager / lead / principal are hard stops at any tier.
    # Exempt phrases are removed first so a job family that merely contains a
    # blocked word is not read as a level; the real modifiers survive, so
    # "Senior Member of Technical Staff" is still caught on "senior".
    seniority_scan = title
    for phrase in c.get("title_exemptions", []):
        seniority_scan = seniority_scan.replace(phrase, " ")

    for term in c["block_title_terms"]:
        if re.search(rf"(?<![a-z]){re.escape(term.strip())}(?![a-z])", seniority_scan):
            return f"seniority: title says '{term.strip()}'", tier

    # A source that grades its own postings is more trustworthy than a regex
    # over the title, so it wins where present.
    hint = job["level_hint"] if "level_hint" in job.keys() else None
    if hint and hint in c.get("block_levels", []):
        return f"seniority: source grades this '{hint}'", tier

    level = title_level(job["title"] or "")
    if level is not None and level >= 2:
        if level > c["stretch_max_level"]:
            return f"seniority: title is level {level}", tier
        tier = "stretch"

    ok, why = location_ok(job["location"], bool(job["remote"]), cfg)
    if not ok:
        return f"location: {why}", tier

    if requires_home_city(c):
        text = (job["location"] or "").lower()
        if not re.search(rf"(?<![a-z])({'|'.join(home_tokens(c))})(?![a-z])", text, re.I):
            return f"location: not {home_city(c)} ({job['location']})", tier

    years = required_years(job["description"], job["title"])
    if years is not None and years > c["max_required_years"]:
        if years > c["stretch_max_years"]:
            return f"experience: needs {years:g}+ years", tier
        tier = "stretch"

    # first_seen as the fallback, or an undated LinkedIn row never ages out.
    age = age_days(job["posted_at"], column(job, "first_seen"))
    if age is not None and age > c["max_age_days"]:
        seen = "posted" if job["posted_at"] else "first seen"
        return f"stale: {seen} {age}d ago", tier

    return None, tier


# ------------------------------------------------------------------ signals


def column(row, name: str):
    """Read a column that may not be in this query's projection.

    sqlite3.Row raises IndexError for a missing column and a plain dict raises
    KeyError, and rows reach the gates from several different SELECTs. Reading
    `first_seen` directly broke nine tests and would have broken any caller
    whose query did not happen to include it.
    """
    try:
        return row[name]
    except (IndexError, KeyError):
        return None


def age_days(posted_at: str | None, first_seen: str | None = None) -> int | None:
    """How old a posting is, falling back to when we first saw it.

    LinkedIn search returns no post date — 32 rows in a live corpus — and
    treating those as ageless let them bypass the staleness gate forever and,
    once scoring decayed by age, float to the very top. `first_seen` is a hard
    lower bound: the posting existed at least that long ago.
    """
    for stamp in (posted_at, first_seen):
        if not stamp:
            continue
        try:
            when = datetime.fromisoformat(stamp)
        except ValueError:
            continue
        # A bare date parses naive. One adapter emitting "2026-08-25" instead
        # of a full timestamp raised TypeError here and killed the entire run,
        # so assume UTC rather than trusting every source to get this right.
        if when.tzinfo is None:
            when = when.replace(tzinfo=UTC)
        return max((datetime.now(UTC) - when).days, 0)
    return None


def _freshness(posted_at: str | None, first_seen: str | None = None) -> tuple[float, str]:
    """Small additive nudge. The decisive age handling is the decay multiplier.

    Uses `age_days` rather than parsing here: this function had its own copy of
    the arithmetic, and a single adapter emitting a naive date crashed the
    ranking of 24,000 postings through it.
    """
    days = age_days(posted_at, first_seen)
    if days is None:
        return 0.0, ""
    if days <= 2:
        return 10.0, f"posted {days}d ago"
    if days <= 7:
        return 7.0, f"posted {days}d ago"
    if days <= 14:
        return 4.0, f"posted {days}d ago"
    return (1.5 if days <= 30 else 0.0), f"posted {days}d ago"


def freshness_multiplier(posted_at: str | None, half_life_days: float = 21.0,
                         first_seen: str | None = None) -> float:
    """How much of a posting's fit is still worth having, given its age.

    Freshness was an additive bonus of at most 10 points, which a strong skill
    match simply outweighed: on a live shortlist the median age was 22 days and
    a 37-day-old posting sat at number two. For a tool whose whole premise is
    applying into short queues, that is the wrong answer no matter how well the
    role fits.

    Decay is the honest shape — the queue keeps growing after a posting goes
    up, so the same role is worth less the later you reach it. Halving every
    three weeks keeps a two-week-old strong match above a fresh weak one,
    without letting a five-week-old role lead the list.
    """
    days = age_days(posted_at, first_seen)
    if days is None:
        # Nothing to go on at all. Deliberately pessimistic: an undated posting
        # scored as fresh outranks every dated one, which is how three
        # undated rows took the top three places on a live shortlist.
        return 0.5 ** 2
    return 0.5 ** (days / max(half_life_days, 1.0))


def _title_match(title: str, targets: dict[str, int]) -> tuple[float, str]:
    best, label = 0, ""
    for phrase, weight in targets.items():
        if phrase in title and weight > best:
            best, label = weight, phrase
    return (15.0 * best / 10.0, f"title matches '{label}'") if best else (0.0, "")


def _skill_overlap(text: str, skills: dict[str, int]) -> tuple[float, list[str]]:
    """Weighted term overlap on a saturating curve.

    Saturating rather than linear so a long keyword-stuffed description cannot
    outrank a focused one that names the four things that actually matter.
    """
    matched = [
        (term, weight)
        for term, weight in skills.items()
        if re.search(rf"(?<![a-z0-9]){re.escape(term)}(?![a-z0-9])", text)
    ]
    if not matched:
        return 0.0, []
    total = sum(w for _, w in matched)
    score = 30.0 * (total / (total + 45.0))
    top = [t for t, _ in sorted(matched, key=lambda kv: -kv[1])[:6]]
    return score, top


def score_job(job: sqlite3.Row, profile_name: str, cfg: dict, *, ats_only: bool,
              delisted: set[str] | None = None) -> Score:
    result = Score(job_id=job["id"], profile=profile_name)

    # `delisted` has to reach here, not just the counting path in run(): these
    # rows are what the shortlist reads. Gating only the counter produced a
    # tidy "delisted 781" line in the stats while every one of them stayed in
    # the digest.
    gate, tier = gate_with_tier(job, cfg, delisted=delisted)
    result.tier = tier
    if gate:
        result.passed = False
        result.gate = gate
        return result

    profile = cfg["profiles"][profile_name]
    signals = cfg["signals"]
    title = (job["title"] or "").lower()
    text = f"{title}\n{(job['description'] or '')}".lower()

    points, reason = _title_match(title, profile["target_titles"])
    result.score += points
    if reason:
        result.reasons.append(reason)

    points, top_skills = _skill_overlap(text, profile["skills"])
    result.score += points
    if top_skills:
        result.reasons.append("skills: " + ", ".join(top_skills))

    points, reason = _freshness(job["posted_at"], column(job, "first_seen"))
    result.score += points
    if reason:
        result.reasons.append(reason)

    # The stated target: infra depth valued inside a product team.
    has_infra = any(t in text for t in signals["infra_terms"])
    has_product = any(t in text for t in signals["product_terms"])
    if has_infra and has_product:
        result.score += 8.0
        result.reasons.append("infra + product scope")
    elif any(t in title for t in signals["pure_ops_titles"]) and not has_product:
        result.score -= 10.0
        result.reasons.append("pure ops role — not the target")

    if any(t in text for t in signals["ai_bonus_terms"]):
        result.score += 6.0
        result.reasons.append("LLM/agent work")

    level = title_level(job["title"] or "")
    if level == 1 or any(
        re.search(rf"(?<![a-z]){re.escape(t)}(?![a-z])", title)
        for t in cfg["constraints"]["boost_title_levels"]
        if len(t) > 2
    ):
        result.score += 8.0
        result.reasons.append("explicitly entry level")

    years = required_years(job["description"], job["title"])
    if years is not None and years <= 1:
        result.score += 5.0
        result.reasons.append(f"asks only {years:g}+ years")
    elif years is None:
        result.score += 2.0
        result.reasons.append("no stated experience bar")

    if ats_only:
        result.score += 5.0
        result.reasons.append("not yet on aggregators")

    points, where = location_tier(job["location"], bool(job["remote"]), cfg)
    result.score += points
    result.reasons.append(where)

    half_life = float(cfg.get("signals", {}).get("freshness_half_life_days", 21))
    decay = freshness_multiplier(job["posted_at"], half_life, column(job, "first_seen"))
    result.score = round(max(result.score, 0.0) * decay, 2)
    return result


# --------------------------------------------------------------------- run


def run(conn: sqlite3.Connection, cfg: dict | None = None) -> dict:
    cfg = cfg or load_config()
    early = db.ats_only_keys(conn)
    gone = db.delisted_ids(conn)
    jobs = conn.execute("SELECT * FROM jobs").fetchall()
    stamp = db.now()

    conn.execute("DELETE FROM scores")
    gates: dict[str, int] = {}
    passed = stretch = 0

    for job in jobs:
        is_early = job["dedup_key"] in early
        for profile_name in cfg["profiles"]:
            score = score_job(job, profile_name, cfg, ats_only=is_early,
                              delisted=gone)
            conn.execute(
                """INSERT INTO scores
                       (job_id, profile, score, passed, tier, gate, reasons, ranked_at)
                   VALUES (?,?,?,?,?,?,?,?)""",
                (
                    score.job_id, score.profile, score.score, int(score.passed),
                    score.tier, score.gate, json.dumps(score.reasons), stamp,
                ),
            )
        # Gates are profile-independent, so evaluate once per posting rather
        # than re-scoring against the first profile purely to count.
        gate, tier = gate_with_tier(job, cfg, delisted=gone)
        if gate:
            gates[gate.split(":")[0]] = gates.get(gate.split(":")[0], 0) + 1
        elif tier == "stretch":
            stretch += 1
        else:
            passed += 1

    conn.commit()
    return {"jobs": len(jobs), "eligible": passed, "stretch": stretch, "gated": gates}


def shortlist(
    conn: sqlite3.Connection, limit: int = 40, tier: str | None = "core",
    per_company: int | None = 6,
) -> list[sqlite3.Row]:
    """Best-scoring profile per posting, one row each, capped per employer.

    ROW_NUMBER rather than `score = MAX(score)`: with the subquery form, a job
    that scores *identically* on both resumes matches twice and is listed
    twice. `profile` is in the tiebreak so the winner is stable across runs.

    `per_company` exists because adding Amazon put 136 of 249 rows on the
    shortlist under one employer — 55% of it, crowding out exactly the smaller
    companies with short queues that the whole pipeline is built to find. A
    company that has ten good openings is still only worth a handful of
    applications, so the cap costs nothing real. Pass None to lift it.
    """
    cap = per_company if per_company and per_company > 0 else 10_000
    return conn.execute(
        """WITH ranked AS (
               SELECT s.job_id, s.profile, s.score, s.reasons, s.tier,
                      ROW_NUMBER() OVER (
                          PARTITION BY s.job_id
                          ORDER BY s.score DESC, s.profile ASC
                      ) AS rn
               FROM scores s WHERE s.passed = 1 AND (? IS NULL OR s.tier = ?)
           ),
           best AS (
               SELECT j.id, j.company, j.title, j.location, j.url, j.posted_at,
                      j.first_seen, j.source,
                      j.remote, j.salary_min, j.salary_max, j.salary_ccy,
                      j.description,
                      r.profile AS profile, r.score AS score,
                      r.reasons AS reasons, r.tier AS tier,
                      ROW_NUMBER() OVER (
                          PARTITION BY j.company
                          ORDER BY r.score DESC, j.posted_at DESC, j.id ASC
                      ) AS company_rn
               FROM ranked r
               JOIN jobs j ON j.id = r.job_id
               WHERE r.rn = 1
           )
           SELECT * FROM best
           WHERE company_rn <= ?
           ORDER BY score DESC, posted_at DESC
           LIMIT ?""",
        (tier, tier, cap, limit),
    ).fetchall()


def both_track_jobs(conn: sqlite3.Connection, threshold: float = 30.0) -> list[sqlite3.Row]:
    """Postings that score well on *both* resumes — the differentiated set."""
    return conn.execute(
        """SELECT j.id, j.company, j.title, j.location, j.url, j.posted_at,
                  MIN(s.score) AS weakest, MAX(s.score) AS best
           FROM jobs j JOIN scores s ON s.job_id = j.id
           WHERE s.passed = 1
           GROUP BY j.id
           HAVING COUNT(*) = 2 AND MIN(s.score) >= ?
           ORDER BY weakest DESC""",
        (threshold,),
    ).fetchall()
