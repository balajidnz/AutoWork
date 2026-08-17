"""SmartRecruiters public postings API — no auth.

    GET https://api.smartrecruiters.com/v1/companies/{token}/postings?limit=100&offset=N
    -> {"offset": N, "limit": 100, "totalFound": T, "content": [...]}

Two things make this adapter different from the others:

1. The list response carries no description, only a `ref` to a per-posting
   detail endpoint. Hydrating all of them would be one request per posting
   across every board — thousands. Only India-eligible postings are hydrated,
   which is where a description actually changes the outcome.

2. There is no 404 for an unknown company. A nonexistent token returns exactly
   what a real company with no openings returns, so `totalFound == 0` has to be
   treated as not-found. The cost is missing a real board that happens to be
   empty on verification day; the alternative is a watchlist full of phantom
   tokens, since every guess would "succeed".
"""

from __future__ import annotations

import asyncio

import httpx

from ..db import Job, slug
from . import BoardNotFound, html_to_text, iso, normalise_level

ATS = "smartrecruiters"
BASE = "https://api.smartrecruiters.com/v1/companies"
PAGE = 100
MAX_PAGES = 12          # 1,200 postings per board is far past any real board
HYDRATE_LIMIT = 60      # bound the detail fan-out per board

_INDIA_CITIES = {
    "bangalore", "bengaluru", "hyderabad", "pune", "chennai", "mumbai",
    "delhi", "new delhi", "gurugram", "gurgaon", "noida", "kolkata",
    "ahmedabad", "jaipur", "kochi", "coimbatore", "indore", "chandigarh",
}


def url_for(token: str, offset: int = 0) -> str:
    return f"{BASE}/{token}/postings?limit={PAGE}&offset={offset}"


def _location(item: dict) -> tuple[str | None, bool]:
    loc = item.get("location") or {}
    remote = bool(loc.get("remote"))
    parts = [loc.get("city"), loc.get("region")]
    country = (loc.get("country") or "").upper()
    if country:
        parts.append(country)
    text = ", ".join(p for p in parts if p) or None
    if remote and text:
        text = f"Remote - {text}"
    elif remote:
        text = "Remote"
    return text, remote


def _india_eligible(item: dict) -> bool:
    """Cheap pre-filter deciding whether a description is worth fetching."""
    loc = item.get("location") or {}
    if (loc.get("country") or "").lower() == "in":
        return True
    if (loc.get("city") or "").strip().lower() in _INDIA_CITIES:
        return True
    return bool(loc.get("remote"))


def posting_url(item: dict, token: str) -> str:
    """Public posting URL.

    Only the per-posting detail response carries `postingUrl`, and the list
    response does not — so this has to be reconstructed for the postings that
    are never hydrated. Both parts matter: the path uses the company's own
    `identifier` casing (SWIGGY, not the lowercase token we probed with) and
    ends in a title slug. Without the slug the page loads but its client-side
    router redirects in a loop, which looks like a broken link rather than a
    malformed one.
    """
    company = (item.get("company") or {}).get("identifier") or token
    title = slug(item.get("name") or "")
    tail = f"{item.get('id')}-{title}" if title else str(item.get("id"))
    return f"https://jobs.smartrecruiters.com/{company}/{tail}"


async def _hydrate(client: httpx.AsyncClient, token: str, item: dict) -> str | None:
    try:
        resp = await client.get(f"{BASE}/{token}/postings/{item['id']}")
        resp.raise_for_status()
        sections = ((resp.json().get("jobAd") or {}).get("sections") or {})
    except (httpx.HTTPError, ValueError, KeyError):
        return None
    # Company boilerplate is deliberately excluded: it is identical across every
    # posting at a company and would dominate keyword scoring.
    chunks = [
        (sections.get(name) or {}).get("text")
        for name in ("jobDescription", "qualifications", "additionalInformation")
    ]
    return html_to_text("\n".join(c for c in chunks if c)) or None


async def fetch(client: httpx.AsyncClient, token: str) -> list[Job]:
    items: list[dict] = []
    for page in range(MAX_PAGES):
        resp = await client.get(url_for(token, page * PAGE))
        if resp.status_code == 404:
            raise BoardNotFound(token)
        resp.raise_for_status()
        payload = resp.json()
        if not isinstance(payload, dict) or "content" not in payload:
            raise BoardNotFound(token)
        batch = payload.get("content") or []
        items.extend(batch)
        if len(items) >= payload.get("totalFound", 0) or len(batch) < PAGE:
            break

    if not items:
        raise BoardNotFound(token)

    hydrate = [i for i in items if _india_eligible(i)][:HYDRATE_LIMIT]
    descriptions = dict(
        zip(
            (i["id"] for i in hydrate),
            await asyncio.gather(*(_hydrate(client, token, i) for i in hydrate)),
        )
    )

    jobs: list[Job] = []
    for item in items:
        location, remote = _location(item)
        company = (item.get("company") or {}).get("name") or token
        jobs.append(
            Job(
                source=ATS,
                source_id=str(item.get("id")),
                company=company,
                company_token=token,
                title=item.get("name") or "",
                url=item.get("postingUrl") or posting_url(item, token),
                location=location,
                remote=remote,
                description=descriptions.get(item.get("id")),
                department=(item.get("department") or {}).get("label")
                or (item.get("function") or {}).get("label"),
                posted_at=iso(item.get("releasedDate")),
                level_hint=normalise_level((item.get("experienceLevel") or {}).get("id")),
                raw={
                    # Structured seniority, unlike every other source where it
                    # has to be inferred from the title.
                    "experience_level": (item.get("experienceLevel") or {}).get("id"),
                    "employment_type": (item.get("typeOfEmployment") or {}).get("label"),
                    "hydrated": item.get("id") in descriptions,
                },
            )
        )
    return jobs
