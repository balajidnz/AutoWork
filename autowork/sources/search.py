"""Search-based sources, via JobSpy.

Structurally different from the ATS adapters, which is why it lives outside
``ADAPTERS``: those enumerate a company's board by token, this issues keyword
searches and gets whatever matches. JobSpy is also synchronous with its own HTTP
stack, so it cannot share the async httpx client.

Measured before building (see README): of JobSpy's eight boards only two return
anything for India. LinkedIn is the better of the pair — it carries a structured
``job_level``, which is a more reliable seniority signal than parsing the title.
Indeed has volume but is noisier and rate-limits after roughly eight queries
from one IP, so it is off by default. Naukri returns ``406 recaptcha required``,
and Glassdoor, Google, ZipRecruiter and Bayt return zero rows for Bangalore.

LinkedIn is scraped **unauthenticated** — no cookie, no session, nothing tied to
an account. That is a different thing from automating actions as a logged-in
user, which is what gets accounts restricted.
"""

from __future__ import annotations

import time
from typing import Any

from ..db import Job
from . import normalise_level

SOURCE = "search"
THROTTLE = 2.0  # seconds between searches


def _iso(value: Any) -> str | None:
    """JobSpy hands back a date, not a timestamp."""
    if not value or str(value) in {"NaT", "nan", "None"}:
        return None
    text = str(value)[:10]
    return f"{text}T00:00:00+00:00" if len(text) == 10 else None


def _clean(value: Any) -> str | None:
    """pandas leaves NaN where a field was absent; it must not reach the DB."""
    if value is None:
        return None
    text = str(value).strip()
    return None if text.lower() in {"", "nan", "none", "nat"} else text


def fetch(config: dict) -> list[Job]:
    """Run every configured search and flatten the results.

    Imported lazily so the rest of the pipeline keeps working — and the tests
    keep running — without pandas and tls-client installed.
    """
    from jobspy import scrape_jobs  # noqa: PLC0415 — heavy, optional at import time

    sites: list[str] = config.get("sites") or ["linkedin"]
    terms: list[str] = config.get("terms") or []
    location: str = config.get("location") or "Bangalore, India"
    wanted = int(config.get("results_per_term", 40))
    hours_old = int(config.get("hours_old", 168))

    jobs: list[Job] = []
    first = True
    for site in sites:
        for term in terms:
            if not first:
                time.sleep(THROTTLE)
            first = False
            options: dict[str, Any] = dict(
                site_name=[site], search_term=term, location=location,
                results_wanted=wanted, hours_old=hours_old,
                description_format="markdown",
            )
            if site == "linkedin":
                # Descriptions are a second request per posting, but the gates
                # need them — the experience bar lives in the description.
                options["linkedin_fetch_description"] = True
            if site in {"indeed", "glassdoor"}:
                options["country_indeed"] = config.get("country", "India")

            try:
                frame = scrape_jobs(**options)
            except Exception as exc:  # noqa: BLE001 — one bad search must not
                # take down the digest; the ATS boards are the primary source.
                print(f"  ! search {site}/{term}: {type(exc).__name__}: {exc}")
                continue

            for _, row in frame.iterrows():
                title = _clean(row.get("title"))
                company = _clean(row.get("company"))
                url = _clean(row.get("job_url"))
                if not (title and company and url):
                    # Staffing listings routinely omit the employer. Without a
                    # company the dedup key is meaningless and the row cannot
                    # be matched against an ATS posting for the same opening.
                    continue
                jobs.append(
                    Job(
                        source=SOURCE,
                        source_id=_clean(row.get("id")) or url,
                        company=company,
                        company_token=site,
                        title=title,
                        url=_clean(row.get("job_url_direct")) or url,
                        location=_clean(row.get("location")),
                        remote=bool(row.get("is_remote")),
                        description=_clean(row.get("description")),
                        posted_at=_iso(row.get("date_posted")),
                        level_hint=normalise_level(_clean(row.get("job_level"))),
                        salary_min=row.get("min_amount") if row.get("min_amount") == row.get("min_amount") else None,
                        salary_max=row.get("max_amount") if row.get("max_amount") == row.get("max_amount") else None,
                        salary_ccy=_clean(row.get("currency")),
                        raw={"site": site, "term": term},
                    )
                )
    return jobs
