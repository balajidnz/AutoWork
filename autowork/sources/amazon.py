"""Amazon's own job search — no auth, no ATS.

    GET https://www.amazon.jobs/en/search.json
        ?normalized_country_code[]=IND&base_query=...&result_limit=...

Amazon, Google, Microsoft, Apple, Netflix and Meta run their own career
portals rather than Greenhouse or Lever, so `autowork verify` resolves none of
them — probing all six against every supported ATS returned nothing. Of the
portals, Amazon's is the one that serves plain JSON to an unauthenticated GET,
so it is the one that gets an adapter.

A caveat worth stating: the whole premise of this project is applying into
short queues, and an Amazon posting is the opposite of that. It is here
because Amazon India hires a lot of SDE-1s, not because it fits the thesis.

`normalized_country_code[]` is the filter that actually works. `country[]` and
`loc_query` both return 10,000 hits led by Kuala Lumpur and Sunnyvale.
"""

from __future__ import annotations

from datetime import UTC, datetime

import httpx

from ..db import Job
from . import html_to_text, looks_remote

SOURCE = "amazon"
BASE = "https://www.amazon.jobs/en/search.json"
# The portal blocks a bare httpx user agent.
HEADERS = {"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                         "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124 Safari/537.36"}
PAGE = 100


def _posted(value: str | None) -> str | None:
    """"August 26, 2026" -> an aware ISO timestamp.

    Timezone-aware on purpose. A bare date parses back as naive, and the age
    arithmetic compares against an aware `now(UTC)` — returning "2026-08-25"
    here raised TypeError inside the gate and took down the whole rank run.
    The portal states no time, so midnight UTC is the honest reading.
    """
    if not value:
        return None
    try:
        naive = datetime.strptime(value.strip(), "%B %d, %Y")
    except ValueError:
        return None
    return naive.replace(tzinfo=UTC).isoformat()


def _job(item: dict) -> Job:
    path = item.get("job_path") or ""
    # basic_qualifications carries the years bar; description carries the rest.
    # Both matter: the gates read years, the scorer reads skills.
    body = "\n\n".join(
        html_to_text(item.get(field) or "")
        for field in ("description", "basic_qualifications", "preferred_qualifications")
        if item.get(field)
    )
    location = item.get("normalized_location") or item.get("location")
    # `company_name` is the legal entity — "ADCI - Karnataka", "ADCI HYD 13
    # SEZ", "ADSIPL - Telangana". Unreadable on a shortlist, and it splits one
    # employer into five, so the duplicate-application guard never connects
    # them and no per-company cap can see them as the same place.
    entity = item.get("company_name") or ""
    return Job(
        source=SOURCE,
        company="Amazon",
        company_token="amazon",
        title=item.get("title") or "",
        url=f"https://www.amazon.jobs{path}" if path else "https://www.amazon.jobs",
        location=location,
        remote=looks_remote(location or ""),
        description=body,
        department=" · ".join(x for x in (entity, item.get("business_category")) if x) or None,
        posted_at=_posted(item.get("posted_date")),
        # The portal states this outright, which beats inferring from a title.
        level_hint="intern" if item.get("is_intern") else None,
        source_id=str(item.get("id_icims") or item.get("id") or path),
        raw={},
    )


def fetch(queries: list[str], country: str = "IND", limit: int = 400) -> list[Job]:
    """Postings for each query term, deduplicated by Amazon's own job id."""
    found: dict[str, Job] = {}
    with httpx.Client(timeout=30.0, headers=HEADERS) as client:
        for query in queries:
            offset = 0
            while offset < limit:
                resp = client.get(BASE, params={
                    "base_query": query,
                    "normalized_country_code[]": country,
                    "result_limit": PAGE,
                    "offset": offset,
                    "sort": "recent",
                })
                resp.raise_for_status()
                items = resp.json().get("jobs") or []
                if not items:
                    break
                for item in items:
                    job = _job(item)
                    found[job.id] = job
                if len(items) < PAGE:
                    break
                offset += PAGE
    return list(found.values())
