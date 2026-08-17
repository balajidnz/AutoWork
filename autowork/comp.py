"""Compensation estimates from AmbitionBox.

Indian job postings essentially never publish salary — measured at 0/40 on both
Indeed and LinkedIn — so the pay question cannot be answered from the listing.
AmbitionBox aggregates self-reported CTC by company and role and exposes it as
`OccupationAggregationByEmployer` JSON-LD, which is structured enough to parse
without rendering the page.

Estimates are cached in `data/comp.json` and committed. Company pay bands move
slowly, so a lookup is worth doing once per quarter rather than once per digest,
and caching keeps the request volume to roughly one per company ever.

These are self-reported figures, not offers. Treat them as a band to sanity-check
against, not a number to negotiate from.
"""

from __future__ import annotations

import json
import re
import time
from dataclasses import asdict, dataclass
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import httpx

from . import db

COMP_JSON = db.DATA_DIR / "comp.json"
BASE = "https://www.ambitionbox.com/salaries"
STALE_AFTER = timedelta(days=90)
THROTTLE = 0.8  # seconds between companies

# AmbitionBox serves a bot page to a default client; a browser UA is enough.
UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
)

_LD = re.compile(r'<script[^>]+application/ld\+json[^>]*>(.*?)</script>', re.S)
LAKH = 100_000

# Our titles are long and specific; AmbitionBox roles are short and canonical.
ROLE_SLUGS: tuple[tuple[re.Pattern, str], ...] = (
    (re.compile(r"\b(sre|site reliability)\b", re.I), "site-reliability-engineer"),
    (re.compile(r"\bdevops\b", re.I), "devops-engineer"),
    (re.compile(r"\b(platform|infrastructure|infra)\b", re.I), "software-engineer"),
    (re.compile(r"\b(data engineer)\b", re.I), "data-engineer"),
    (re.compile(r"\b(machine learning|ml engineer)\b", re.I), "machine-learning-engineer"),
    (re.compile(r"\b(frontend|front[- ]end)\b", re.I), "frontend-developer"),
    (re.compile(r"\b(backend|back[- ]end)\b", re.I), "backend-developer"),
    (re.compile(r"\b(full[- ]?stack)\b", re.I), "full-stack-software-developer"),
)


@dataclass(slots=True)
class Comp:
    company: str
    role: str
    p25: float | None = None
    median: float | None = None
    p75: float | None = None
    sample: int | None = None
    exp_min: int | None = None
    exp_max: int | None = None
    source: str | None = None
    fetched: str = ""
    found: bool = False

    @property
    def confident(self) -> bool:
        """A handful of self-reports is a rumour, not a range."""
        return self.found and (self.sample or 0) >= 15

    def summary(self, floor_lpa: float | None = None) -> str:
        if not self.found:
            return "no comp data"
        parts = [f"₹{self.median:.0f}L median"]
        if self.p25 and self.p75:
            parts.append(f"({self.p25:.0f}–{self.p75:.0f}L)")
        parts.append(f"n={self.sample}")
        if not self.confident:
            parts.append("· thin sample")
        if floor_lpa and self.median and self.median < floor_lpa:
            parts.append(f"· BELOW your ₹{floor_lpa:.0f}L")
        return " ".join(parts)


def role_slug(title: str) -> str:
    for pattern, slug in ROLE_SLUGS:
        if pattern.search(title):
            return slug
    return "software-engineer"


def company_slug(company: str) -> str:
    """AmbitionBox slugs are hyphenated names, so keep the words the dedup slug
    strips — "Hevo Data" is `hevo-data`, not `hevo`."""
    return re.sub(r"[^a-z0-9]+", "-", (company or "").lower()).strip("-")


def url_for(company: str, title: str) -> str:
    return f"{BASE}/{company_slug(company)}-salaries/{role_slug(title)}"


def _parse(html: str) -> dict | None:
    for block in _LD.findall(html):
        try:
            data = json.loads(block.strip())
        except json.JSONDecodeError:
            continue
        if isinstance(data, dict) and data.get("@type") == "OccupationAggregationByEmployer":
            return data
    return None


def _lakhs(value) -> float | None:
    try:
        return round(float(value) / LAKH, 1)
    except (TypeError, ValueError):
        return None


def _candidates(company: str, title: str) -> list[tuple[str, str]]:
    """(company-slug, role-slug) pairs to try, most specific first.

    Two fallbacks earn their keep. The specific role often 404s where the
    generic one resolves — GitLab has a `software-engineer` page but no
    `backend-developer` one. And Lever and Ashby do not echo a display name, so
    the company arrives as its board token: `hevodata` rather than `Hevo Data`,
    which needs the hyphen restored.
    """
    companies = [company_slug(company)]
    display = _display_name(company)
    if display and company_slug(display) not in companies:
        companies.append(company_slug(display))

    roles = [role_slug(title)]
    if "software-engineer" not in roles:
        roles.append("software-engineer")
    return [(c, r) for c in companies for r in roles]


def _display_name(company: str) -> str | None:
    """Recover the human name for a board token, via the verified watchlist."""
    try:
        conn = db.connect()
        row = conn.execute(
            "SELECT company FROM boards WHERE lower(token) = ? AND company IS NOT NULL",
            (company.lower(),),
        ).fetchone()
    except Exception:  # noqa: BLE001 — enrichment must never break a run
        return None
    return row["company"] if row else None


def fetch(client: httpx.Client, company: str, title: str) -> Comp:
    result = Comp(
        company=company, role=role_slug(title),
        source=url_for(company, title), fetched=date.today().isoformat(),
    )
    data = None
    for company_part, role_part in _candidates(company, title):
        url = f"{BASE}/{company_part}-salaries/{role_part}"
        try:
            resp = client.get(url)
        except (httpx.HTTPError, ValueError):
            continue
        if resp.status_code != 200:
            continue
        data = _parse(resp.text)
        if data:
            result.source, result.role = url, role_part
            break
    if not data:
        return result

    salary = (data.get("estimatedSalary") or [{}])[0]
    result.found = True
    result.p25 = _lakhs(salary.get("percentile25"))
    result.median = _lakhs(salary.get("median"))
    result.p75 = _lakhs(salary.get("percentile75"))
    try:
        result.sample = int(data.get("sampleSize") or 0)
    except (TypeError, ValueError):
        result.sample = None
    result.exp_min = data.get("yearsExperienceMin")
    result.exp_max = data.get("yearsExperienceMax")
    return result


# ------------------------------------------------------------------- cache


def load(path: Path = COMP_JSON) -> dict[str, Comp]:
    if not path.exists():
        return {}
    raw = json.loads(path.read_text(encoding="utf-8"))
    return {k: Comp(**v) for k, v in raw.items()}


def save(cache: dict[str, Comp], path: Path = COMP_JSON) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {k: asdict(v) for k, v in sorted(cache.items())}
    path.write_text(
        json.dumps(payload, indent=2, sort_keys=True, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )


def key(company: str, title: str) -> str:
    return f"{company_slug(company)}|{role_slug(title)}"


def _stale(entry: Comp) -> bool:
    try:
        age = datetime.now(UTC).date() - date.fromisoformat(entry.fetched)
    except ValueError:
        return True
    return age > STALE_AFTER


def lookup(cache: dict[str, Comp], company: str, title: str) -> Comp | None:
    return cache.get(key(company, title))


def enrich(pairs: list[tuple[str, str]], cache: dict[str, Comp] | None = None) -> dict[str, Comp]:
    """Fill the cache for (company, title) pairs it does not already cover.

    Misses are cached too. A company with no AmbitionBox page will never have
    one, and re-requesting it every morning is wasted traffic on someone else's
    server for a result we already know.
    """
    cache = cache if cache is not None else load()
    todo = [
        (c, t) for c, t in pairs
        if key(c, t) not in cache or _stale(cache[key(c, t)])
    ]
    if not todo:
        return cache

    with httpx.Client(
        timeout=25.0, follow_redirects=True, headers={"User-Agent": UA}
    ) as client:
        for index, (company, title) in enumerate(todo):
            # Throttled deliberately. With two company and two role candidates
            # each, a full cold cache is a few hundred requests, and firing
            # them back-to-back gets the whole batch soft-blocked — the first
            # attempt at this returned 0 hits for exactly that reason. This is
            # someone else's server and the work is cached for 90 days, so
            # slow is free.
            if index:
                time.sleep(THROTTLE)
            cache[key(company, title)] = fetch(client, company, title)
    return cache
