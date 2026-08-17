"""Compensation estimate parsing and slug resolution."""

from __future__ import annotations

import json

import pytest

from autowork import comp

# Trimmed from a real AmbitionBox response.
PAGE = """
<html><head>
<script type="application/ld+json">{"@type":"Organization","name":"Swiggy"}</script>
<script type="application/ld+json">
{"@context":"http://schema.googleapis.com/",
 "@type":"OccupationAggregationByEmployer",
 "name":"Software Engineer",
 "yearsExperienceMin":0,"yearsExperienceMax":4,
 "estimatedSalary":[{"@type":"MonetaryAmountDistribution","currency":"INR",
   "median":"2480614.40","percentile25":"1600000","percentile75":"2925000"}],
 "sampleSize":"144"}
</script>
</head></html>
"""


def test_parses_percentiles_into_lakhs():
    data = comp._parse(PAGE)
    assert data is not None
    salary = data["estimatedSalary"][0]
    assert comp._lakhs(salary["median"]) == 24.8
    assert comp._lakhs(salary["percentile25"]) == 16.0
    assert comp._lakhs(salary["percentile75"]) == 29.2


def test_parse_ignores_other_ld_blocks():
    """The page carries Organization and BreadcrumbList blocks too."""
    assert comp._parse('<script type="application/ld+json">{"@type":"Organization"}</script>') is None


def test_parse_survives_malformed_json():
    assert comp._parse('<script type="application/ld+json">{not json</script>') is None


@pytest.mark.parametrize(
    "title,expected",
    [
        ("Site Reliability Engineer", "site-reliability-engineer"),
        ("SDE III - Devops", "devops-engineer"),
        ("Backend Engineer, Payments", "backend-developer"),
        ("Intermediate Fullstack Engineer", "full-stack-software-developer"),
        ("AI Platform Engineer", "software-engineer"),
        ("Software Dev Engineer I", "software-engineer"),
    ],
)
def test_role_slug(title, expected):
    assert comp.role_slug(title) == expected


@pytest.mark.parametrize(
    "company,expected",
    [
        ("Hevo Data", "hevo-data"),
        ("Observe.AI", "observe-ai"),
        ("SWIGGY", "swiggy"),
        ("Together AI", "together-ai"),
    ],
)
def test_company_slug_keeps_words(company, expected):
    """Unlike the dedup slug, this must not strip words — AmbitionBox's URL for
    "Hevo Data" is hevo-data, and collapsing it to hevo 404s."""
    assert comp.company_slug(company) == expected


def test_candidates_add_a_generic_role_fallback():
    """GitLab has a software-engineer page but no backend-developer one."""
    pairs = comp._candidates("GitLab", "Backend Engineer")
    assert ("gitlab", "backend-developer") in pairs
    assert ("gitlab", "software-engineer") in pairs
    assert pairs.index(("gitlab", "backend-developer")) < pairs.index(("gitlab", "software-engineer"))


def test_candidates_do_not_duplicate_when_role_is_already_generic():
    assert comp._candidates("Swiggy", "Software Dev Engineer I") == [("swiggy", "software-engineer")]


# ------------------------------------------------------------------ summary


def _comp(**over) -> comp.Comp:
    base = dict(company="Acme", role="software-engineer", found=True,
                median=24.8, p25=16.0, p75=29.2, sample=144)
    return comp.Comp(**{**base, **over})


def test_summary_reports_band_and_sample():
    assert _comp().summary() == "₹25L median (16–29L) n=144"


def test_summary_flags_below_floor():
    assert "BELOW your ₹17L" in _comp(median=12.9).summary(17)
    assert "BELOW" not in _comp(median=24.8).summary(17)


def test_summary_flags_thin_sample():
    """Eight self-reports is a rumour, not a range."""
    assert _comp(sample=8).confident is False
    assert "thin sample" in _comp(sample=8).summary()
    assert _comp(sample=144).confident is True


def test_missing_data_is_explicit():
    assert comp.Comp(company="Acme", role="x").summary() == "no comp data"


# -------------------------------------------------------------------- cache


def test_cache_round_trips(tmp_path):
    path = tmp_path / "comp.json"
    original = {"acme|software-engineer": _comp()}
    comp.save(original, path)
    loaded = comp.load(path)
    assert loaded["acme|software-engineer"].median == 24.8
    assert json.loads(path.read_text())["acme|software-engineer"]["sample"] == 144


def test_missing_cache_file_is_empty(tmp_path):
    assert comp.load(tmp_path / "absent.json") == {}


def test_stale_entries_are_refetched():
    assert comp._stale(_comp(fetched="2020-01-01")) is True
    assert comp._stale(_comp(fetched="")) is True


def test_key_is_company_and_role():
    assert comp.key("Hevo Data", "SDE I") == "hevo-data|software-engineer"
