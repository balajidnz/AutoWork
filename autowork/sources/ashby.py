"""Ashby public posting API — no auth.

    GET https://api.ashbyhq.com/posting-api/job-board/{token}?includeCompensation=true
    -> {"jobs": [...], "apiVersion": "..."}
"""

from __future__ import annotations

import httpx

from ..db import Job
from . import BoardNotFound, html_to_text, iso, looks_remote, parse_salary

ATS = "ashby"
BASE = "https://api.ashbyhq.com/posting-api/job-board"


def url_for(token: str) -> str:
    return f"{BASE}/{token}?includeCompensation=true"


async def fetch(client: httpx.AsyncClient, token: str) -> list[Job]:
    resp = await client.get(url_for(token))
    if resp.status_code in (400, 404):
        raise BoardNotFound(token)
    resp.raise_for_status()
    payload = resp.json()
    if not isinstance(payload, dict) or "jobs" not in payload:
        raise BoardNotFound(token)

    jobs: list[Job] = []
    for item in payload.get("jobs", []):
        # isListed=False is a posting Ashby is hosting but not publishing.
        if item.get("isListed") is False:
            continue
        location = item.get("location")
        secondary = ", ".join(
            s.get("location", "") for s in item.get("secondaryLocations") or []
            if isinstance(s, dict) and s.get("location")
        )
        comp = item.get("compensation") or {}
        smin, smax, ccy = parse_salary(comp.get("compensationTierSummary"))
        jobs.append(
            Job(
                source=ATS,
                source_id=str(item.get("id")),
                company=token,
                company_token=token,
                title=item.get("title") or "",
                url=item.get("jobUrl") or item.get("applyUrl") or "",
                location=location or secondary or None,
                # isRemote and workplaceType are both frequently null, so fall
                # back to reading the location string.
                remote=bool(item.get("isRemote"))
                or (item.get("workplaceType") or "").lower() == "remote"
                or looks_remote(location, secondary),
                description=item.get("descriptionPlain")
                or html_to_text(item.get("descriptionHtml")),
                department=item.get("department") or item.get("team"),
                posted_at=iso(item.get("publishedAt")),
                salary_min=smin,
                salary_max=smax,
                salary_ccy=ccy,
                raw={
                    "apply_url": item.get("applyUrl"),
                    "employment_type": item.get("employmentType"),
                    "compensation_summary": comp.get("compensationTierSummary"),
                },
            )
        )
    return jobs
