"""Fetch every verified board concurrently and land the postings in SQLite."""

from __future__ import annotations

import asyncio
import sqlite3
from dataclasses import dataclass

import httpx

from . import db, rank
from .sources import BoardNotFound, search
from .watchlist import ADAPTERS, USER_AGENT


@dataclass(slots=True)
class BoardResult:
    ats: str
    token: str
    jobs: list[db.Job]
    error: str | None = None


async def _fetch_board(
    client: httpx.AsyncClient, sem: asyncio.Semaphore, ats: str, token: str
) -> BoardResult:
    adapter = ADAPTERS[ats]
    async with sem:
        for attempt in range(3):
            try:
                jobs = await adapter.fetch(client, token)
            except BoardNotFound:
                return BoardResult(ats, token, [], "not_found")
            except httpx.HTTPStatusError as exc:
                status = exc.response.status_code
                if status in (429, 500, 502, 503, 504) and attempt < 2:
                    await asyncio.sleep(2 ** attempt)
                    continue
                return BoardResult(ats, token, [], f"http_{status}")
            except (httpx.HTTPError, ValueError) as exc:
                if attempt < 2:
                    await asyncio.sleep(2 ** attempt)
                    continue
                return BoardResult(ats, token, [], type(exc).__name__)
            else:
                return BoardResult(ats, token, jobs)
    return BoardResult(ats, token, [], "exhausted")


async def poll_boards(
    boards: list[tuple[str, str]], *, concurrency: int = 12
) -> list[BoardResult]:
    sem = asyncio.Semaphore(concurrency)
    limits = httpx.Limits(max_connections=concurrency, max_keepalive_connections=concurrency)
    async with httpx.AsyncClient(
        timeout=httpx.Timeout(30.0, connect=10.0),
        headers={"User-Agent": USER_AGENT},
        limits=limits,
        follow_redirects=True,
    ) as client:
        return await asyncio.gather(
            *(_fetch_board(client, sem, ats, token) for ats, token in boards)
        )


def run(conn: sqlite3.Connection, *, concurrency: int = 12) -> dict:
    if not db.verified_boards(conn):
        # Fresh database (or CI checkout) — reload the committed watchlist
        # rather than re-probing every candidate token.
        db.import_boards(conn)
    boards = [(r["ats"], r["token"]) for r in db.verified_boards(conn)]
    if not boards:
        return {"boards": 0, "fetched": 0, "new": 0, "updated": 0, "errors": []}

    results = asyncio.run(poll_boards(boards, concurrency=concurrency))

    all_jobs: list[db.Job] = []
    errors: list[str] = []
    for result in results:
        if result.error:
            errors.append(f"{result.ats}:{result.token} -> {result.error}")
            # Record the failure but leave verified_at intact, so a bad day
            # never silently drops a company from the watchlist.
            db.record_board(conn, result.ats, result.token, ok=False, error=result.error)
        else:
            all_jobs.extend(result.jobs)
            db.record_board(
                conn, result.ats, result.token, ok=True, job_count=len(result.jobs)
            )

    # Keyword search runs after the boards: the ATS postings are the primary
    # source, and if a search fails the digest should still go out.
    search_cfg = (rank.load_config().get("job_search") or {})
    searched = 0
    if search_cfg.get("enabled"):
        try:
            found = search.fetch(search_cfg)
            all_jobs.extend(found)
            searched = len(found)
        except Exception as exc:  # noqa: BLE001
            errors.append(f"search -> {type(exc).__name__}: {exc}")

    new, updated = db.upsert_jobs(conn, all_jobs)
    return {
        "searched": searched,
        "boards": len(boards),
        "fetched": len(all_jobs),
        "new": new,
        "updated": updated,
        "errors": errors,
    }
