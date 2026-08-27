"""Contact extraction: who is worth writing to, and who only looks like it."""

from __future__ import annotations

import pytest

from autowork import contact


@pytest.mark.parametrize(
    "text,people,generic",
    [
        ("Reach out to priya.sharma@swiggy.com", ["priya.sharma@swiggy.com"], []),
        # a queue is a real inbox but writing to it is just the application again
        ("Apply via careers@atlan.com", [], ["careers@atlan.com"]),
        ("Contact talentacquisition@dexcom.com", [], ["talentacquisition@dexcom.com"]),
        # placeholders inside prose, observed in real postings
        ("email name@cvent.com to apply", [], []),
        ("write to yourname@company.com", [], []),
        # automated senders and unrelated departments
        ("no-reply@example.org", [], []),
        ("accommodations@harvey.ai for accessibility", [], []),
        ("legal@acme.com", [], []),
        # ATS plumbing, not the employer
        ("apply at jobs@greenhouse.io", [], []),
        ("", [], []),
        (None, [], []),
    ],
)
def test_email_classification(text, people, generic):
    assert contact.emails_in(text) == (people, generic)


def test_duplicates_collapse_case_insensitively():
    people, _ = contact.emails_in("a.b@x.com and A.B@X.com")
    assert len(people) == 1


# ------------------------------------------------------------------ domain


def test_domain_prefers_an_address_in_the_description():
    """The domain the employer actually sends from beats any inference."""
    assert contact.domain_for("Acme", "https://boards.greenhouse.io/acme",
                              "mail hr@acme-corp.in") == "acme-corp.in"


def test_domain_ignores_ats_hosts():
    """The apply URL is usually the ATS, not the employer."""
    assert contact.domain_for("Swiggy", "https://jobs.smartrecruiters.com/SWIGGY/1",
                              None) == "swiggy.com"


def test_domain_uses_a_real_company_host_when_present():
    assert contact.domain_for("Acme", "https://www.acme.io/careers/1", None) == "acme.io"


def test_domain_falls_back_to_the_company_name():
    assert contact.domain_for("Hevo Data", None, None) == "hevodata.com"


# ----------------------------------------------------------------- guesses


def test_guesses_cover_the_common_patterns():
    c = contact.Contact(company="Swiggy", domain="swiggy.com", mx=True)
    out = c.guesses("Priya", "Sharma")
    assert out[0] == "priya.sharma@swiggy.com"
    assert "psharma@swiggy.com" in out
    assert len(out) == len(set(out))


def test_no_guesses_without_a_deliverable_domain():
    """Offering an address for a domain that bounces is worse than offering
    none — the mail fails silently and the role moves on."""
    assert contact.Contact(company="X", domain="x.com", mx=False).guesses("A", "B") == []
    assert contact.Contact(company="X", domain=None, mx=True).guesses("A", "B") == []


def test_single_name_skips_surname_patterns():
    c = contact.Contact(company="X", domain="x.com", mx=True)
    assert c.guesses("Priya") == ["priya@x.com"]


# ----------------------------------------------------------------- summary


def test_summary_distinguishes_person_from_queue():
    base = dict(company="X", domain="x.com", mx=True)
    assert contact.Contact(**base, found_emails=["a.b@x.com"]).summary().startswith("person:")
    assert contact.Contact(**base, generic_emails=["careers@x.com"]).summary().startswith("queue only:")
    assert "accepts mail" in contact.Contact(**base).summary()
    assert "bounce" in contact.Contact(company="X", domain="x.com", mx=False).summary()


def test_best_prefers_a_person():
    c = contact.Contact(company="X", found_emails=["a@x.com"], generic_emails=["careers@x.com"])
    assert c.best == "a@x.com"
    assert contact.Contact(company="X", generic_emails=["careers@x.com"]).best == "careers@x.com"
    assert contact.Contact(company="X").best is None


def test_cache_round_trips(tmp_path):
    path = tmp_path / "c.json"
    contact.save({"x": contact.Contact(company="X", domain="x.com", mx=True,
                                       generic_emails=["careers@x.com"])}, path)
    assert contact.load(path)["x"].generic_emails == ["careers@x.com"]
    assert contact.load(tmp_path / "missing.json") == {}


def test_ats_hosts_are_not_mistaken_for_the_employer():
    """Ashby serves from `jobs.ashbyhq.com`, so a pattern anchored on `ashby\\.`
    never matched and one company's contact domain resolved to the applicant
    tracking system instead of to the company."""
    for host in ("jobs.ashbyhq.com", "boards.greenhouse.io", "jobs.lever.co",
                 "myworkdayjobs.com", "api.smartrecruiters.com"):
        assert contact._NOISE_DOMAIN.search(host + "."), host


def test_a_company_whose_name_starts_like_an_ats_is_kept():
    """The first fix used a wildcard suffix, which would have filtered any
    employer whose domain merely begins with a vendor's name."""
    for host in ("levercorp.com", "leverage.io", "ashbygroup.co.uk", "indeedhq.com"):
        assert not contact._NOISE_DOMAIN.search(host + "."), host
