"""Make the suite independent of whose laptop it runs on.

`profile/profiles.json` carries a salary and target titles, so it is
gitignored — which means a clean checkout has no config at all and the gate
tests, which nearly all call `rank.load_config()`, failed with 59 errors on
CI while passing locally.

A test suite should not depend on one person's profile anyway. This builds a
synthetic one from the shipped template whenever the real file is absent, so
the same tests run on a contributor's fork, on CI, and here.
"""

from __future__ import annotations

import pytest

from autowork import profile_build, rank

# The values are not arbitrary. Existing tests assert on the home-city label
# ("Bangalore", not "Bengaluru") and on which skills the resumes evidence, so
# the synthetic profile has to state the same things the real one did.
SYNTHETIC = {
    "name": "Test Person",
    "city": "Bangalore, India",
    "years": 1.5,
    "experience_band": "0-2",
    "current_ctc_lpa": 17,
    "remote_ok": True,
    "home_only": False,
    "families": ["engineering"],
    "resumes": [{
        "slug": "infra", "label": "Infra", "path": "profile/resume-infra.md",
        "roles": ["Platform Engineer", "DevOps Engineer", "Software Engineer"],
        "skills": ["Terraform", "Kubernetes", "AWS", "Go", "Python",
                   "Ruby on Rails", "Vue", "PostgreSQL", "Redis", "Kafka",
                   "Docker", "CI/CD", "REST APIs", "Microservices"],
        "key_skills": ["Terraform", "Kubernetes"],
        # Azure is deliberately absent: a test asserts it is *not* evidenced.
        "text": ("terraform kubernetes aws go python ruby on rails vue "
                 "postgresql redis kafka docker ci/cd rest apis microservices"),
    }],
}


@pytest.fixture(scope="session", autouse=True)
def _profile(tmp_path_factory):
    """Point the ranker at a real config, generating one if none is checked out."""
    if rank.PROFILES_PATH.exists():
        yield rank.PROFILES_PATH
        return

    target = tmp_path_factory.mktemp("profile") / "profiles.json"
    # Written through the same builder the setup wizard uses, so the fixture
    # cannot drift into a shape the application would never produce.
    original, profile_build.RESUME_DIR = profile_build.RESUME_DIR, target.parent
    try:
        profile_build.save(profile_build.build(SYNTHETIC), target)
    finally:
        profile_build.RESUME_DIR = original

    previous, rank.PROFILES_PATH = rank.PROFILES_PATH, target
    try:
        yield target
    finally:
        rank.PROFILES_PATH = previous
