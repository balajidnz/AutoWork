"""Greenhouse public Job Board API — no auth.

    GET https://boards-api.greenhouse.io/v1/boards/{token}/jobs?content=true
    -> {"jobs": [...], "meta": {"total": N}}
"""

from __future__ import annotations

import httpx

from ..db import Job
from . import BoardNotFound, html_to_text, iso, looks_remote

ATS = "greenhouse"
BASE = "https://boards-api.greenhouse.io/v1/boards"


def url_for(token: str, *, content: bool = True) -> str:
    return f"{BASE}/{token}/jobs" + ("?content=true" if content else "")


async def fetch(client: httpx.AsyncClient, token: str) -> list[Job]:
    resp = await client.get(url_for(token))
    if resp.status_code == 404:
        raise BoardNotFound(token)
    resp.raise_for_status()
    payload = resp.json()

    jobs: list[Job] = []
    for item in payload.get("jobs", []):
        location = (item.get("location") or {}).get("name")
        offices = ", ".join(
            o.get("name", "") for o in item.get("offices") or [] if o.get("name")
        )
        departments = ", ".join(
            d.get("name", "") for d in item.get("departments") or [] if d.get("name")
        )
        jobs.append(
            Job(
                source=ATS,
                source_id=str(item.get("id")),
                company=item.get("company_name") or token,
                company_token=token,
                title=item.get("title") or "",
                url=item.get("absolute_url") or "",
                location=location or offices or None,
                remote=looks_remote(location, offices),
                description=html_to_text(item.get("content")),
                department=departments or None,
                # first_published is when candidates could first see it;
                # updated_at moves on any edit, so it overstates freshness.
                posted_at=iso(item.get("first_published") or item.get("updated_at")),
                raw={"requisition_id": item.get("requisition_id")},
            )
        )
    return jobs
