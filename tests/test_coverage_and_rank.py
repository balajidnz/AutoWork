"""Coverage analysis, plus the SQL-level shortlist behaviour."""

from __future__ import annotations

import pytest

from autowork import coverage as cov
from autowork import db, rank


# --------------------------------------------------------------- coverage


OWNED = {"Kubernetes", "Go", "Terraform", "PostgreSQL", "CI/CD", "AI Agents"}


@pytest.mark.parametrize(
    "jd,term",
    [
        ("Requirements: experience with K8s in production", "Kubernetes"),
        ("Requirements: strong Golang background", "Go"),
        ("Requirements: familiar with continuous integration", "CI/CD"),
        ("Requirements: you have built agentic systems", "AI Agents"),
        ("Requirements: Postgres tuning", "PostgreSQL"),
    ],
)
def test_aliases_count_as_the_canonical_term(jd, term):
    """A resume saying "Kubernetes" and a JD saying "K8s" are one skill to a
    reader and two strings to a search box."""
    assert term in cov.analyse(jd, OWNED).have


def test_missing_terms_are_reported():
    result = cov.analyse("Requirements: deep Java and Azure experience", OWNED)
    assert set(result.missing) == {"Java", "Azure"}
    assert result.have == []


def test_terms_with_regex_metacharacters():
    """C++ and C# break a naive \\b word boundary — '+' and '#' are not word
    characters, so a trailing \\b never matches."""
    result = cov.analyse("Requirements: C++ and C# both used here", set())
    assert "C++" in result.missing and "C#" in result.missing


def test_scanning_prefers_the_requirements_section():
    """A term in the company blurb is not a requirement."""
    jd = (
        "About us: our Java-based legacy platform serves millions.\n"
        "Requirements:\n- Strong Go and Kubernetes experience"
    )
    result = cov.analyse(jd, OWNED)
    assert "Go" in result.have and "Kubernetes" in result.have
    assert "Java" not in result.missing


def test_whole_description_scanned_when_no_section_heading():
    result = cov.analyse("You will write Go and deploy with Terraform.", OWNED)
    assert {"Go", "Terraform"} <= set(result.have)


def test_ratio_and_summary():
    empty = cov.analyse(None, OWNED)
    assert empty.total == 0 and empty.ratio == 1.0
    assert "no explicit requirements" in empty.summary()

    partial = cov.analyse("Requirements: Go, Java", OWNED)
    assert partial.ratio == pytest.approx(0.5)
    assert "missing: Java" in partial.summary()


def test_candidate_terms_reflect_the_resumes():
    owned = cov.candidate_terms(rank.load_config())
    # things both resumes evidence heavily
    assert {"Go", "Kubernetes", "Terraform", "Ruby on Rails", "AWS"} <= owned
    # something neither resume claims
    assert "Azure" not in owned


# ------------------------------------------------------- shortlist / SQL


@pytest.fixture
def seeded(tmp_path):
    conn = db.connect(tmp_path / "t.db")
    job = db.Job(
        source="greenhouse", source_id="1", company="Acme",
        title="Backend Engineer", url="https://example.com/1",
        location="Bengaluru, India",
    )
    db.upsert_jobs(conn, [job])
    return conn, job


def test_tied_scores_yield_one_row_per_job(seeded):
    """A posting scoring identically on both resumes matched twice under the
    old `score = MAX(score)` subquery and appeared twice in the digest."""
    conn, job = seeded
    for profile in ("infra", "product"):
        conn.execute(
            """INSERT INTO scores (job_id, profile, score, passed, tier, reasons, ranked_at)
               VALUES (?,?,?,1,'core','[]',?)""",
            (job.id, profile, 42.0, db.now()),
        )
    conn.commit()

    rows = rank.shortlist(conn, 100, tier=None)
    assert len(rows) == 1
    # the tiebreak is deterministic, so reruns do not shuffle the winner
    assert rows[0]["profile"] == "infra"


def test_shortlist_filters_by_tier(seeded):
    conn, job = seeded
    conn.execute(
        """INSERT INTO scores (job_id, profile, score, passed, tier, reasons, ranked_at)
           VALUES (?,'infra',10,1,'stretch','[]',?)""",
        (job.id, db.now()),
    )
    conn.commit()
    assert rank.shortlist(conn, 100, tier="core") == []
    assert len(rank.shortlist(conn, 100, tier="stretch")) == 1
    assert len(rank.shortlist(conn, 100, tier=None)) == 1


def test_gated_jobs_never_appear(seeded):
    conn, job = seeded
    conn.execute(
        """INSERT INTO scores (job_id, profile, score, passed, tier, gate, reasons, ranked_at)
           VALUES (?,'infra',99,0,'core','seniority','[]',?)""",
        (job.id, db.now()),
    )
    conn.commit()
    assert rank.shortlist(conn, 100, tier=None) == []


def test_ats_only_detection(seeded):
    """The low-competition signal: seen on an ATS but not yet on any aggregator."""
    conn, job = seeded
    assert job.dedup_key in db.ats_only_keys(conn)

    conn.execute(
        "INSERT INTO sightings (dedup_key, source, seen_at) VALUES (?,'adzuna',?)",
        (job.dedup_key, db.now()),
    )
    conn.commit()
    assert job.dedup_key not in db.ats_only_keys(conn)


def test_upsert_is_idempotent(seeded):
    conn, job = seeded
    new, updated = db.upsert_jobs(conn, [job])
    assert (new, updated) == (0, 1)
    assert conn.execute("SELECT COUNT(*) n FROM jobs").fetchone()["n"] == 1
