"""Discover and verify ATS board tokens.

Board tokens are derived from company names, so we generate plausible slugs and
confirm each against the live API. Verification is cheap (one unauthenticated
GET) and the result is permanent, so the watchlist only ever grows.
"""

from __future__ import annotations

import asyncio
import re
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path

import httpx

from . import db
from .sources import BoardNotFound, ashby, greenhouse, lever, smartrecruiters

# Order matters: verification stops at the first ATS that resolves a company,
# so the cheapest and highest-hit-rate boards go first. SmartRecruiters is last
# because it cannot 404 on a bad token and needs a full page fetch to decide.
ADAPTERS = {
    greenhouse.ATS: greenhouse,
    lever.ATS: lever,
    ashby.ATS: ashby,
    smartrecruiters.ATS: smartrecruiters,
}

COMPANIES_FILE = db.DATA_DIR / "companies.txt"

# Words that are part of a legal name but almost never part of a board token.
_DROP = {
    "inc", "llc", "ltd", "limited", "corp", "corporation", "co",
    "technologies", "technology", "tech", "labs", "lab", "software",
    "systems", "solutions", "india", "pvt", "private", "gmbh", "group",
    "holdings", "the",
}

USER_AGENT = "autowork/0.1 (personal job search; +https://github.com/)"


def token_variants(name: str) -> list[str]:
    """Plausible board tokens for a company name, most likely first.

    'Scale AI' -> ['scaleai', 'scale-ai', 'scale']
    """
    words = [w for w in re.split(r"[^A-Za-z0-9]+", name.lower()) if w]
    if not words:
        return []
    meaningful = [w for w in words if w not in _DROP] or words

    seen: dict[str, None] = {}
    for candidate in (
        "".join(meaningful),
        "-".join(meaningful),
        "".join(words),
        meaningful[0],
    ):
        if candidate and candidate not in seen:
            seen[candidate] = None
    return list(seen)


def load_companies(path: Path = COMPANIES_FILE) -> list[str]:
    if not path.exists():
        return []
    return [
        line.strip()
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.startswith("#")
    ]


@dataclass(slots=True)
class Probe:
    ats: str
    token: str
    company: str
    ok: bool = False
    job_count: int = 0
    error: str | None = None


async def _probe(
    client: httpx.AsyncClient,
    sem: asyncio.Semaphore,
    ats: str,
    token: str,
    company: str,
) -> Probe:
    adapter = ADAPTERS[ats]
    result = Probe(ats=ats, token=token, company=company)
    async with sem:
        for attempt in range(3):
            try:
                jobs = await adapter.fetch(client, token)
            except BoardNotFound:
                result.error = "not_found"
                return result
            except httpx.HTTPStatusError as exc:
                status = exc.response.status_code
                if status in (429, 500, 502, 503, 504) and attempt < 2:
                    # Back off rather than discard: a 429 here would otherwise
                    # be recorded as "no such board" and lost permanently.
                    await asyncio.sleep(2 ** attempt)
                    continue
                result.error = f"http_{status}"
                return result
            except (httpx.HTTPError, ValueError) as exc:
                if attempt < 2:
                    await asyncio.sleep(2 ** attempt)
                    continue
                result.error = type(exc).__name__
                return result
            else:
                result.ok = True
                result.job_count = len(jobs)
                return result
    return result


async def verify(
    companies: Iterable[str],
    *,
    ats_list: Iterable[str] = tuple(ADAPTERS),
    concurrency: int = 12,
    stop_on_first_hit: bool = True,
) -> list[Probe]:
    """Probe every (company, ats, token-variant) combination.

    With ``stop_on_first_hit`` a company that resolves on one ATS is not probed
    on the others — companies use exactly one, and skipping the rest cuts the
    request count by roughly two thirds.
    """
    companies = list(companies)
    sem = asyncio.Semaphore(concurrency)
    limits = httpx.Limits(max_connections=concurrency, max_keepalive_connections=concurrency)
    hits: list[Probe] = []

    async with httpx.AsyncClient(
        timeout=httpx.Timeout(20.0, connect=10.0),
        headers={"User-Agent": USER_AGENT},
        limits=limits,
        follow_redirects=True,
    ) as client:
        for ats in ats_list:
            resolved = {p.company for p in hits} if stop_on_first_hit else set()
            pending = [
                (ats, token, company)
                for company in companies
                if company not in resolved
                for token in token_variants(company)
            ]
            results = await asyncio.gather(
                *(_probe(client, sem, a, t, c) for a, t, c in pending)
            )
            # One company can match several variants; keep the fullest board.
            best: dict[str, Probe] = {}
            for probe in results:
                if not probe.ok:
                    continue
                current = best.get(probe.company)
                if current is None or probe.job_count > current.job_count:
                    best[probe.company] = probe
            hits.extend(best.values())
    return hits


def persist(conn, probes: Iterable[Probe]) -> int:
    count = 0
    for probe in probes:
        db.record_board(
            conn,
            probe.ats,
            probe.token,
            company=probe.company,
            ok=probe.ok,
            job_count=probe.job_count,
            error=probe.error,
        )
        count += 1
    return count
