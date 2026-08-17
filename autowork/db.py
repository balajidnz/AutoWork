"""SQLite store for job postings.

The database is a derived artifact: it is rebuilt from ``data/jobs.jsonl`` and
``data/status.json``, which are what actually get committed. That keeps the
GitHub Actions run and the local console in sync through diffable text files
instead of a binary blob.
"""

from __future__ import annotations

import json
import re
import sqlite3
import unicodedata
from collections.abc import Iterable, Iterator
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = REPO_ROOT / "data"

# Ephemeral: rebuilt from scratch by `autowork poll` in ~15s, so it is never
# committed. A full corpus dump is ~85MB, which git should not be carrying.
DB_PATH = DATA_DIR / "autowork.db"
JOBS_JSONL = DATA_DIR / "jobs.jsonl"

# Committed: small, diffable, and either unregenerable or expensive to rebuild.
SEEN_TXT = DATA_DIR / "seen.txt"
STATUS_JSON = DATA_DIR / "status.json"
DIGEST_DIR = DATA_DIR / "digest"
BOARDS_JSON = DATA_DIR / "boards.json"

# Sources that publish straight from a company's own applicant tracking system.
# A posting seen *only* from one of these has not reached the aggregators yet,
# which is our stand-in for "low applicant count".
ATS_SOURCES = frozenset({"greenhouse", "lever", "ashby", "workable", "smartrecruiters"})

SCHEMA = """
CREATE TABLE IF NOT EXISTS jobs (
    id            TEXT PRIMARY KEY,
    dedup_key     TEXT NOT NULL,
    source        TEXT NOT NULL,
    company       TEXT NOT NULL,
    company_token TEXT,
    title         TEXT NOT NULL,
    location      TEXT,
    remote        INTEGER NOT NULL DEFAULT 0,
    url           TEXT NOT NULL,
    description   TEXT,
    department    TEXT,
    posted_at     TEXT,
    -- Seniority as the source states it, where the source states it. Beats
    -- inferring a level from the title string.
    level_hint    TEXT,
    first_seen    TEXT NOT NULL,
    last_seen     TEXT NOT NULL,
    salary_min    REAL,
    salary_max    REAL,
    salary_ccy    TEXT,
    raw           TEXT
);
CREATE INDEX IF NOT EXISTS idx_jobs_dedup   ON jobs(dedup_key);
CREATE INDEX IF NOT EXISTS idx_jobs_posted  ON jobs(posted_at DESC);

-- One row per (posting, source) pair. Lets us tell an ATS-only posting from
-- one that has already been syndicated to LinkedIn/Adzuna/etc.
CREATE TABLE IF NOT EXISTS sightings (
    dedup_key TEXT NOT NULL,
    source    TEXT NOT NULL,
    seen_at   TEXT NOT NULL,
    PRIMARY KEY (dedup_key, source)
);

-- Scores are per (job, profile): the same posting is a different proposition
-- depending on which resume you send. Recomputed in full on every rank run,
-- so this table is disposable.
CREATE TABLE IF NOT EXISTS scores (
    job_id   TEXT NOT NULL,
    profile  TEXT NOT NULL,
    score    REAL NOT NULL,
    passed   INTEGER NOT NULL,
    -- 'core' clears every bar; 'stretch' is a near-miss on seniority worth
    -- applying to anyway. Kept distinct so the digest can label it.
    tier     TEXT NOT NULL DEFAULT 'core',
    gate     TEXT,
    reasons  TEXT,
    ranked_at TEXT NOT NULL,
    PRIMARY KEY (job_id, profile)
);
CREATE INDEX IF NOT EXISTS idx_scores_rank ON scores(passed, score DESC);

CREATE TABLE IF NOT EXISTS status (
    job_id     TEXT PRIMARY KEY,
    state      TEXT NOT NULL,
    note       TEXT,
    updated_at TEXT NOT NULL
);

-- Verified ATS board tokens. Grows over time and is never pruned on failure,
-- only marked, so a transient outage does not lose the entry.
CREATE TABLE IF NOT EXISTS boards (
    ats          TEXT NOT NULL,
    token        TEXT NOT NULL,
    company      TEXT,
    verified_at  TEXT,
    last_ok      TEXT,
    last_error   TEXT,
    job_count    INTEGER,
    PRIMARY KEY (ats, token)
);
"""

_SLUG_STRIP = re.compile(r"[^a-z0-9]+")
_COMPANY_NOISE = re.compile(
    r"\b(inc|llc|ltd|limited|corp|corporation|technologies|technology|labs|"
    r"software|systems|solutions|india|pvt|private|gmbh|holdings|group|co)\b"
)


def now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def slug(value: str | None) -> str:
    if not value:
        return ""
    folded = unicodedata.normalize("NFKD", value).encode("ascii", "ignore").decode()
    return _SLUG_STRIP.sub("-", folded.lower()).strip("-")


def company_slug(value: str | None) -> str:
    """Normalise a company name so "Acme Technologies Pvt Ltd" == "Acme"."""
    base = slug(value).replace("-", " ")
    base = _COMPANY_NOISE.sub(" ", base)
    return _SLUG_STRIP.sub("-", base.strip()).strip("-")


def _tidy(value: str | None) -> str | None:
    """Collapse all runs of whitespace — including NBSP and newlines — to one space."""
    if value is None:
        return None
    return " ".join(value.split()) or None


@dataclass(slots=True)
class Job:
    source: str
    company: str
    title: str
    url: str
    company_token: str | None = None
    location: str | None = None
    remote: bool = False
    description: str | None = None
    department: str | None = None
    posted_at: str | None = None
    level_hint: str | None = None
    salary_min: float | None = None
    salary_max: float | None = None
    salary_ccy: str | None = None
    source_id: str | None = None
    raw: dict = field(default_factory=dict)

    def __post_init__(self) -> None:
        """Collapse whitespace in the display fields.

        Boards routinely carry a trailing space or a non-breaking space inside
        the title ("Software Engineer - Hypervisor "), which is invisible until
        it reaches something that treats it as syntax: markdown bold never
        closes, so the console renders literal asterisks. It also weakens the
        seniority regex, which matches on token boundaries. Normalising here
        fixes every source and every output channel at once — 8.5% of the
        corpus was affected.
        """
        self.company = _tidy(self.company) or ""
        self.title = _tidy(self.title) or ""
        # Optional fields collapse to None when they held only whitespace, so
        # `location or "—"` in the renderers does the right thing.
        self.location = _tidy(self.location)
        self.department = _tidy(self.department)

    @property
    def id(self) -> str:
        return f"{self.source}:{self.source_id or slug(self.url)}"

    @property
    def dedup_key(self) -> str:
        """Identifies the same opening across sources.

        Location is deliberately excluded: the same role is routinely listed as
        "Bengaluru", "Bangalore, India" and "Remote - India" on different
        boards, and treating those as distinct openings defeats the purpose.
        """
        return f"{company_slug(self.company)}|{slug(self.title)}"


def connect(path: Path = DB_PATH) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA)
    return conn


def upsert_jobs(conn: sqlite3.Connection, jobs: Iterable[Job]) -> tuple[int, int]:
    """Insert or refresh postings. Returns (new, updated)."""
    stamp = now()
    new = updated = 0
    for job in jobs:
        row = conn.execute("SELECT 1 FROM jobs WHERE id = ?", (job.id,)).fetchone()
        if row:
            # Refresh the mutable fields, not just the timestamp. A row is
            # written once and then re-seen for weeks, so touching only
            # last_seen freezes whatever the board said the first day: an
            # employer's edit to the title, location or salary never lands,
            # and a parsing fix never reaches the rows already stored.
            # first_seen, id and url stay put — they are the row's identity.
            conn.execute(
                """UPDATE jobs SET last_seen = ?, dedup_key = ?, company = ?,
                       title = ?, location = ?, remote = ?, description = ?,
                       department = ?, posted_at = ?, level_hint = ?,
                       salary_min = ?, salary_max = ?, salary_ccy = ?
                   WHERE id = ?""",
                (
                    stamp, job.dedup_key, job.company, job.title, job.location,
                    int(job.remote), job.description, job.department, job.posted_at,
                    job.level_hint, job.salary_min, job.salary_max, job.salary_ccy,
                    job.id,
                ),
            )
            updated += 1
        else:
            conn.execute(
                """INSERT INTO jobs (
                       id, dedup_key, source, company, company_token, title,
                       location, remote, url, description, department, posted_at,
                       level_hint, first_seen, last_seen, salary_min, salary_max,
                       salary_ccy, raw
                   ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    job.id, job.dedup_key, job.source, job.company, job.company_token,
                    job.title, job.location, int(job.remote), job.url, job.description,
                    job.department, job.posted_at, job.level_hint, stamp, stamp,
                    job.salary_min, job.salary_max, job.salary_ccy,
                    json.dumps(job.raw, ensure_ascii=False),
                ),
            )
            new += 1
        conn.execute(
            """INSERT INTO sightings (dedup_key, source, seen_at) VALUES (?,?,?)
               ON CONFLICT(dedup_key, source) DO NOTHING""",
            (job.dedup_key, job.source, stamp),
        )
    conn.commit()
    return new, updated


def ats_only_keys(conn: sqlite3.Connection) -> set[str]:
    """Postings no aggregator has picked up yet — the early-signal set."""
    rows = conn.execute(
        "SELECT dedup_key, GROUP_CONCAT(source) AS srcs FROM sightings GROUP BY dedup_key"
    ).fetchall()
    return {
        r["dedup_key"]
        for r in rows
        if set((r["srcs"] or "").split(",")) <= ATS_SOURCES
    }


def record_board(
    conn: sqlite3.Connection,
    ats: str,
    token: str,
    *,
    company: str | None = None,
    ok: bool,
    job_count: int | None = None,
    error: str | None = None,
) -> None:
    stamp = now()
    conn.execute(
        """INSERT INTO boards (ats, token, company, verified_at, last_ok, last_error, job_count)
           VALUES (?,?,?,?,?,?,?)
           ON CONFLICT(ats, token) DO UPDATE SET
               company     = COALESCE(excluded.company, boards.company),
               verified_at = COALESCE(boards.verified_at, excluded.verified_at),
               last_ok     = COALESCE(excluded.last_ok, boards.last_ok),
               last_error  = excluded.last_error,
               job_count   = COALESCE(excluded.job_count, boards.job_count)""",
        (ats, token, company, stamp if ok else None, stamp if ok else None,
         error, job_count),
    )
    conn.commit()


def export_boards(conn: sqlite3.Connection, path: Path = BOARDS_JSON) -> int:
    """Persist the verified watchlist.

    Technically regenerable from companies.txt, but only by re-probing several
    hundred URLs against third-party APIs. Committing it keeps a fresh clone or
    a CI run from doing that on every invocation.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    rows = [
        {"ats": r["ats"], "token": r["token"], "company": r["company"]}
        for r in verified_boards(conn)
    ]
    path.write_text(
        json.dumps(rows, indent=2, sort_keys=True, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    return len(rows)


def import_boards(conn: sqlite3.Connection, path: Path = BOARDS_JSON) -> int:
    """Reload the watchlist into a freshly built database."""
    if not path.exists():
        return 0
    rows = json.loads(path.read_text(encoding="utf-8"))
    for row in rows:
        record_board(conn, row["ats"], row["token"], company=row.get("company"), ok=True)
    return len(rows)


def verified_boards(conn: sqlite3.Connection, ats: str | None = None) -> list[sqlite3.Row]:
    sql = "SELECT * FROM boards WHERE verified_at IS NOT NULL"
    params: tuple = ()
    if ats:
        sql += " AND ats = ?"
        params = (ats,)
    return conn.execute(sql + " ORDER BY ats, token", params).fetchall()


def export_jsonl(conn: sqlite3.Connection, path: Path = JOBS_JSONL) -> int:
    """Dump the full corpus for local inspection. Not committed — see SEEN_TXT."""
    path.parent.mkdir(parents=True, exist_ok=True)
    rows = conn.execute("SELECT * FROM jobs ORDER BY id").fetchall()
    with path.open("w", encoding="utf-8") as fh:
        for row in rows:
            record = dict(row)
            record.pop("raw", None)
            fh.write(json.dumps(record, ensure_ascii=False) + "\n")
    return len(rows)


def iter_jsonl(path: Path = JOBS_JSONL) -> Iterator[dict]:
    if not path.exists():
        return
    with path.open(encoding="utf-8") as fh:
        for line in fh:
            if line.strip():
                yield json.loads(line)


def load_seen(path: Path = SEEN_TXT) -> set[str]:
    """Job ids already surfaced in a past digest."""
    if not path.exists():
        return set()
    return {
        line.strip()
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    }


def mark_seen(job_ids: Iterable[str], path: Path = SEEN_TXT) -> int:
    """Add ids to the dedup ledger.

    Owned by the digest, not the poller. The ledger's job is to answer "have I
    already shown you this?", so writing it at poll time would mark every
    posting as seen before the digest ever looked at it, and the digest would
    be empty forever after the first run.

    This is also the one piece of state a fresh checkout cannot reconstruct:
    the database is rebuilt from scratch each run, so `first_seen` is always
    "now" in CI. Sorted, so the daily diff is purely appended lines.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    ids = load_seen(path) | set(job_ids)
    path.write_text("\n".join(sorted(ids)) + "\n", encoding="utf-8")
    return len(ids)


def export_digest(rows: Iterable[dict], day: str, directory: Path = DIGEST_DIR) -> Path:
    """Write one day's shortlist. Descriptions are clipped: the digest is for
    deciding what to open, and the full text is a fetch away behind the URL."""
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"{day}.jsonl"
    with path.open("w", encoding="utf-8") as fh:
        for row in rows:
            record = dict(row)
            record.pop("raw", None)
            if record.get("description"):
                record["description"] = record["description"][:1200]
            fh.write(json.dumps(record, ensure_ascii=False) + "\n")
    return path


def load_status(path: Path = STATUS_JSON) -> dict[str, dict]:
    if not path.exists():
        return {}
    return json.loads(path.read_text(encoding="utf-8"))


def save_status(states: dict[str, dict], path: Path = STATUS_JSON) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(states, indent=2, sort_keys=True, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )


def set_state(conn: sqlite3.Connection, job_id: str, state: str, note: str | None = None) -> None:
    conn.execute(
        """INSERT INTO status (job_id, state, note, updated_at) VALUES (?,?,?,?)
           ON CONFLICT(job_id) DO UPDATE SET
               state = excluded.state, note = excluded.note, updated_at = excluded.updated_at""",
        (job_id, state, note, now()),
    )
    conn.commit()
    states = load_status()
    states[job_id] = {"state": state, "note": note, "updated_at": now()}
    save_status(states)


def job_dict(job: Job) -> dict:
    d = asdict(job)
    d["id"] = job.id
    d["dedup_key"] = job.dedup_key
    return d
