"""Lever public postings API — no auth.

    GET https://api.lever.co/v0/postings/{token}?mode=json
    -> [ {...}, ... ]   (a bare list, not an envelope)
"""

from __future__ import annotations

import httpx

from ..db import Job
from . import BoardNotFound, html_to_text, iso, looks_remote, parse_salary

ATS = "lever"
BASE = "https://api.lever.co/v0/postings"


def url_for(token: str) -> str:
    return f"{BASE}/{token}?mode=json"


async def fetch(client: httpx.AsyncClient, token: str) -> list[Job]:
    resp = await client.get(url_for(token))
    # Lever answers an unknown site with 404, and occasionally 400.
    if resp.status_code in (400, 404):
        raise BoardNotFound(token)
    resp.raise_for_status()
    payload = resp.json()
    if not isinstance(payload, list):
        raise BoardNotFound(token)

    jobs: list[Job] = []
    for item in payload:
        cats = item.get("categories") or {}
        location = cats.get("location")
        workplace = item.get("workplaceType")
        description = item.get("descriptionPlain") or html_to_text(item.get("description"))
        smin, smax, ccy = parse_salary(cats.get("salaryRange") and str(cats["salaryRange"]))
        jobs.append(
            Job(
                source=ATS,
                source_id=str(item.get("id")),
                company=token,  # Lever does not echo a display name
                company_token=token,
                title=item.get("text") or "",
                # hostedUrl is the human-readable posting; applyUrl skips
                # straight to the form and is what the console opens.
                url=item.get("hostedUrl") or item.get("applyUrl") or "",
                location=location,
                remote=(workplace or "").lower() == "remote" or looks_remote(location),
                description=description,
                department=cats.get("department") or cats.get("team"),
                posted_at=iso(item.get("createdAt")),
                salary_min=smin,
                salary_max=smax,
                salary_ccy=ccy,
                raw={
                    "apply_url": item.get("applyUrl"),
                    "commitment": cats.get("commitment"),
                    "workplace_type": workplace,
                    "country": item.get("country"),
                },
            )
        )
    return jobs
