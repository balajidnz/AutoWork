"""The web console: the plain-language layer, the payload, and the server.

Covers the failure modes that only show up when a person looks at the screen —
a reason reaching the reader in the ranker's own vocabulary, a rank number that
means something different to `/tailor` than to the row it sits on, a resume
label that resolves to nothing.
"""

from __future__ import annotations

import json
import pathlib

import pytest

from autowork import db, present, profile_build, rank, resume_parse, track


@pytest.fixture(autouse=True)
def _isolate_resume_writes(tmp_path, monkeypatch):
    """Never let a test write into the real profile/ directory.

    `profile_build.build` saves each resume's text so tailoring can read it
    back. The multi-resume test uses the slugs infra/product/agentic — the same
    names as the real resumes — and truncated all three to fixture text on its
    first run. Redirect the destination for every test in this module.
    """
    monkeypatch.setattr(profile_build, "RESUME_DIR", tmp_path / "profile")

# ---------------------------------------------------------- vocabulary layer

# Every reason string rank.py can emit. Listed here rather than imported so
# that adding one without a translation fails loudly, instead of quietly
# showing the reader the pipeline's own wording.
RANKER_REASONS = [
    "title matches 'devops engineer'",
    "skills: terraform, eks, argo cd",
    "posted 0d ago",
    "posted 1d ago",
    "posted 12d ago",
    "infra + product scope",
    "pure ops role — not the target",
    "LLM/agent work",
    "explicitly entry level",
    "asks only 1+ years",
    "no stated experience bar",
    "not yet on aggregators",
    "Bangalore",
    "remote",
    "elsewhere in India",
]


@pytest.mark.parametrize("reason", RANKER_REASONS)
def test_every_ranker_reason_is_translated(reason: str) -> None:
    icon, text = present.humanise(reason)
    assert icon != "•", f"no plain-language wording for {reason!r}"
    assert text != reason, f"{reason!r} reaches the reader unchanged"


def test_unknown_reason_survives_untranslated() -> None:
    """A new reason in rank.py must degrade to plain text, never vanish."""
    assert present.humanise("some brand new signal") == ("•", "some brand new signal")


def test_singular_and_plural_days() -> None:
    assert present.humanise("posted 1d ago")[1] == "Posted 1 day ago"
    assert present.humanise("posted 3d ago")[1] == "Posted 3 days ago"
    assert present.humanise("posted 0d ago")[1] == "Posted today"


@pytest.mark.parametrize(
    "score,expected", [(70, "Strong match"), (60, "Strong match"),
                       (59, "Good match"), (45, "Good match"),
                       (44, "Worth a look"), (30, "Worth a look"),
                       (29, "Long shot"), (0, "Long shot")],
)
def test_score_bands(score: float, expected: str) -> None:
    assert present.match_band(score)[0] == expected


def test_every_pipeline_state_has_a_button_label() -> None:
    offered = {s for state in ("", *track.PIPELINE) for s in track.next_states(state)}
    assert offered <= set(present.ACTIONS), (
        f"no button wording for: {offered - set(present.ACTIONS)}"
    )


def test_every_state_has_a_readable_label() -> None:
    assert set(track.PIPELINE) <= set(present.STATE_LABELS)


def test_resume_map_uses_configured_labels_for_new_profiles() -> None:
    """A user who set up with one resume must not be told to send a blank."""
    mapping = present.resume_map({"profiles": {
        "my-cv": {"label": "My CV", "resume": "cv.pdf"},
        "infra": {"label": "ignored", "resume": "ignored.pdf"},
    }})
    assert mapping["my-cv"] == ("My CV", "cv.pdf")
    # The original two keep their friendlier hand-written names.
    assert mapping["infra"] == ("DevOps-leaning", "profile/resume-infra.md")


# ------------------------------------------------------------------ payload


def _corpus_or_skip():
    conn = db.connect()
    if not conn.execute("SELECT 1 FROM jobs LIMIT 1").fetchone():
        pytest.skip("no corpus in this checkout")
    return conn


def test_payload_position_matches_what_tailor_indexes() -> None:
    """The number on a row must be the number `/tailor` resolves.

    The console filters and re-sorts; `autowork show` and `autowork tailor`
    index the unfiltered, score-ordered list. If `position` were the display
    index, searching or re-sorting would silently point `/tailor 3` at a
    different job than the row showing 3.
    """
    conn = _corpus_or_skip()
    payload = present.build(conn)
    canonical = rank.shortlist(conn, 10_000, tier=None)
    for job in payload["jobs"][:25]:
        assert job["id"] == canonical[job["position"] - 1]["id"]


def test_payload_is_json_serialisable() -> None:
    """It crosses the wire; a stray sqlite3.Row or Coverage would 500 the page."""
    json.dumps(present.build(_corpus_or_skip()))


def test_freshness_reason_is_not_repeated_in_the_detail() -> None:
    """The row header already states the age, from live data rather than frozen."""
    for job in present.build(_corpus_or_skip())["jobs"][:20]:
        assert not any(r["icon"] == "🕐" for r in job["reasons"])


# ------------------------------------------------------------ resume parsing


def test_overlapping_employment_is_not_double_counted() -> None:
    """A promotion listed as two rows at one company is one span of time."""
    text = "Engineer, Jan 2023 - Jan 2025\nSenior Engineer, Jan 2024 - Jan 2025"
    assert resume_parse.years_in(text) == 2.0


def test_earliest_job_is_counted_when_listed_last() -> None:
    """Regression: seeding the merge from document order dropped the oldest job.

    Resumes list the current role first, so the earliest span is last — it was
    silently discarded and 21 months of experience read as 15.
    """
    text = "Engineer, Jan 2024 - Jan 2026\nIntern, Jan 2023 - Jan 2024"
    assert resume_parse.years_in(text) == 3.0


def test_name_survives_a_markdown_heading() -> None:
    assert resume_parse._name_in("# Priya Sharma — backend\n") == "Priya Sharma"
    assert resume_parse._name_in("Priya Sharma\npriya@x.com") == "Priya Sharma"


def test_contact_lines_are_not_mistaken_for_a_name() -> None:
    assert resume_parse._name_in("+91 98765 43210 | priya@x.com") == ""


def test_roles_are_ranked_and_capped() -> None:
    """One passing mention must not become a target title."""
    text = ("Full stack engineer. Full stack work. Full stack delivery. "
            "Collaborated with the machine learning team once.")
    roles = resume_parse.roles_in(text)
    assert roles[0] == "Full Stack Engineer"
    assert len(roles) <= 3


def test_plain_text_resume_needs_no_pdf_parser() -> None:
    assert "hello" in resume_parse.extract_text(b"hello world", "cv.md")


def test_skills_come_from_the_shared_vocabulary() -> None:
    """Extraction and gap analysis must agree on what a skill is called.

    If they diverged, a posting could ask for a skill the resume has and still
    be reported as a gap.
    """
    from autowork import coverage

    found = resume_parse.skills_in("Built services in Python on Kubernetes with Terraform.")
    assert {"Python", "Kubernetes", "Terraform"} <= set(found)
    assert set(found) <= set(coverage.VOCABULARY)


# ---------------------------------------------------------- profile building


def _answers(**over):
    base = {
        "name": "Priya Sharma", "city": "Pune, India", "years": 1.5,
        "experience_band": "0-2", "current_ctc_lpa": 12,
        "remote_ok": True, "home_only": False,
        "resumes": [{"slug": "main", "label": "My CV", "path": "cv.pdf",
                     "roles": ["Backend Engineer"], "skills": ["Python", "Django"],
                     "key_skills": ["Django"], "text": "python python python django"}],
    }
    return {**base, **over}


def test_generated_config_is_valid_and_complete() -> None:
    config = profile_build.build(_answers())
    assert profile_build.validate(config) == []
    # The calibrated gates come along for free — that is the point of the split.
    for key in ("block_role_terms", "india_tokens", "title_exemptions", "block_levels"):
        assert key in config["constraints"], f"{key} missing from the template"
    assert config["signals"], "signals must survive from the template"


def test_generated_config_targets_the_users_city_not_bangalore() -> None:
    config = profile_build.build(_answers())
    assert config["constraints"]["home_city_name"] == "Pune"
    assert "pune" in config["constraints"]["home_city_tokens"]
    assert rank.location_tier("Pune, India", False, config) == (12.0, "Pune")
    assert rank.location_tier("Bengaluru, India", False, config)[1] == "elsewhere in India"


def test_starred_skills_outrank_merely_frequent_ones() -> None:
    """Frequency measures what a resume talks about, not what you want next."""
    weights = profile_build.weighted(
        ["Python", "Django"], "python python python django", priority=["Django"]
    )
    assert weights["django"] == 10
    assert weights["python"] < 10


def test_multiple_resumes_become_multiple_profiles() -> None:
    """Several resumes is the normal case — each is scored separately."""
    config = profile_build.build(_answers(resumes=[
        {"slug": "infra", "label": "Infra", "path": "a.pdf", "roles": ["DevOps Engineer"],
         "skills": ["Terraform"], "text": "terraform"},
        {"slug": "product", "label": "Product", "path": "b.pdf", "roles": ["Backend Engineer"],
         "skills": ["Python"], "text": "python"},
    ]))
    assert list(config["profiles"]) == ["infra", "product"]
    assert config["profiles"]["infra"]["preferred"] is True
    assert config["profiles"]["product"]["preferred"] is False


def test_validate_catches_an_empty_setup() -> None:
    assert profile_build.validate({"profiles": {}})
    assert profile_build.validate(profile_build.build(_answers(resumes=[])))


def test_saving_backs_up_an_existing_config(tmp_path) -> None:
    """A hand-tuned config must survive someone re-running setup."""
    target = tmp_path / "profiles.json"
    profile_build.save({"profiles": {"old": {}}}, target)
    profile_build.save({"profiles": {"new": {}}}, target)
    backups = list(tmp_path.glob("profiles.backup-*.json"))
    assert len(backups) == 1
    assert json.loads(backups[0].read_text())["profiles"] == {"old": {}}


@pytest.mark.parametrize("city,expected", [
    ("Bengaluru, India", "bangalore"), ("Bangalore", "bengaluru"),
    ("Mumbai", "bombay"), ("Gurgaon", "gurugram"),
])
def test_city_aliases(city: str, expected: str) -> None:
    """Boards spell the same city several ways."""
    assert expected in profile_build.city_tokens(city)


def test_unknown_city_still_works() -> None:
    assert profile_build.city_tokens("Mysuru, India") == ["mysuru"]


# --------------------------------------------------------- home-city rename


def test_old_bangalore_keys_still_load() -> None:
    """An existing config predates the rename and must keep working."""
    legacy = {"bangalore_tokens": ["bengaluru"], "require_bangalore": True}
    assert rank.home_tokens(legacy) == ["bengaluru"]
    assert rank.requires_home_city(legacy) is True
    assert rank.home_city(legacy) == "Bangalore"


def test_new_keys_win_over_old() -> None:
    both = {"bangalore_tokens": ["bengaluru"], "home_city_tokens": ["pune"],
            "home_city_name": "Pune"}
    assert rank.home_tokens(both) == ["pune"]
    assert rank.home_city(both) == "Pune"


# ------------------------------------------------------------------- tailor


def test_tailor_prompt_forbids_invention() -> None:
    """The truthfulness constraint is the whole point; it must not drift out."""
    from autowork import tailor

    assert "may NOT invent" in tailor.PROMPT
    assert "loses the interview" in tailor.PROMPT


def test_tailor_rejects_an_out_of_range_rank() -> None:
    from autowork import tailor

    with pytest.raises(ValueError, match="pick 1"):
        tailor.build_prompt(_corpus_or_skip(), 99999)


def test_tailor_prompt_carries_the_job_and_the_gaps() -> None:
    from autowork import tailor

    prompt, ctx = tailor.build_prompt(_corpus_or_skip(), 1)
    assert ctx["company"] in prompt
    assert ctx["title"] in prompt


def test_ollama_absent_is_not_an_error() -> None:
    """Most people will not have it running; that must not raise."""
    from autowork import tailor

    assert isinstance(tailor.ollama_models(), list)


# ------------------------------------------------------------------- server


def test_web_assets_ship_with_the_package() -> None:
    """`autowork console` reads these at runtime from wherever it is installed."""
    from autowork import server

    assert (server.WEB / "index.html").exists()
    assert profile_build.TEMPLATE.exists()


def test_server_binds_loopback_only() -> None:
    """It serves a resume and a job history with no authentication."""
    import inspect

    from autowork import server

    assert '"127.0.0.1"' in inspect.getsource(server.serve)


def test_page_makes_no_external_requests() -> None:
    """It has to work offline, and must not leak a job search to a CDN."""
    from autowork import server

    html = (server.WEB / "index.html").read_text(encoding="utf-8")
    for marker in ("//cdn", "<script src", "<link rel=\"stylesheet\""):
        assert marker not in html, f"page reaches outside for {marker!r}"


# ------------------------------------------------- tailoring from the page


def _ctx(**over):
    base = {"title": "SDE-1", "company": "Acme", "profile": "main",
            "resume": "/repo/cv.md", "missing": ["Java"], "url": "https://e.com",
            "position": 7}
    return {**base, **over}


def test_runner_script_is_valid_sh(tmp_path, monkeypatch) -> None:
    """It is handed to /bin/sh; a quoting slip is a broken terminal window."""
    import subprocess

    from autowork import tailor

    monkeypatch.setattr(tailor, "TAILOR_DIR", tmp_path)
    prompt = tmp_path / "p.md"
    prompt.write_text("x")
    script = tailor.runner_script(prompt, _ctx(), "claude")
    assert subprocess.run(["sh", "-n", str(script)], capture_output=True).returncode == 0


def test_runner_script_survives_quotes_in_a_job_title(tmp_path, monkeypatch) -> None:
    """Board titles contain apostrophes — "Developer's Advocate" must not break out."""
    import subprocess

    from autowork import tailor

    monkeypatch.setattr(tailor, "TAILOR_DIR", tmp_path)
    prompt = tmp_path / "p.md"
    prompt.write_text("x")
    script = tailor.runner_script(
        prompt, _ctx(title="Developer's Advocate; echo pwned", company="O'Brien Ltd"), "claude"
    )
    assert subprocess.run(["sh", "-n", str(script)], capture_output=True).returncode == 0
    assert "echo pwned" not in script.read_text().split("read _")[1]


def test_runner_script_is_named_per_posting(tmp_path, monkeypatch) -> None:
    """Two roles tailored in quick succession must not overwrite each other."""
    from autowork import tailor

    monkeypatch.setattr(tailor, "TAILOR_DIR", tmp_path)
    prompt = tmp_path / "p.md"
    prompt.write_text("x")
    first = tailor.runner_script(prompt, _ctx(position=1), "claude")
    second = tailor.runner_script(prompt, _ctx(position=2), "claude")
    assert first != second
    assert first.exists() and second.exists()


def test_ollama_script_uses_the_cli_not_claude(tmp_path, monkeypatch) -> None:
    from autowork import tailor

    monkeypatch.setattr(tailor, "TAILOR_DIR", tmp_path)
    prompt = tmp_path / "p.md"
    prompt.write_text("x")
    body = tailor.runner_script(prompt, _ctx(), "ollama", "llama3.1").read_text()
    assert "--ollama llama3.1" in body
    assert "claude" not in body


def test_script_waits_rather_than_running_immediately(tmp_path, monkeypatch) -> None:
    """Clicking a button must not silently start rewriting a resume."""
    from autowork import tailor

    monkeypatch.setattr(tailor, "TAILOR_DIR", tmp_path)
    prompt = tmp_path / "p.md"
    prompt.write_text("x")
    body = tailor.runner_script(prompt, _ctx(), "claude").read_text()
    assert "read _" in body
    assert body.index("read _") < body.index("exec ")


# ------------------------------------------------------------ request auth


class _FakeHeaders(dict):
    def get(self, key, default=None):  # noqa: D102
        return super().get(key, default)


def _handler_with(headers):
    from autowork import server

    handler = server.Handler.__new__(server.Handler)
    handler.headers = _FakeHeaders(headers)
    return handler


def test_write_without_the_token_is_refused() -> None:
    """Any site you visit can POST to localhost, and one endpoint spawns a shell."""
    assert _handler_with({})._authorised() is False
    assert _handler_with({"X-AutoWork-Token": "guessed"})._authorised() is False


def test_write_with_the_token_is_allowed() -> None:
    from autowork import server

    assert _handler_with({"X-AutoWork-Token": server.TOKEN})._authorised() is True


def test_token_alone_is_not_enough_cross_origin() -> None:
    from autowork import server

    handler = _handler_with({"X-AutoWork-Token": server.TOKEN,
                             "Origin": "https://evil.example"})
    assert handler._authorised() is False


def test_token_is_per_run_and_not_guessable() -> None:
    from autowork import server

    assert len(server.TOKEN) >= 24


def test_page_carries_the_token_placeholder() -> None:
    """Substituted at serve time; a stale literal would lock every write out."""
    from autowork import server

    html = (server.WEB / "index.html").read_text(encoding="utf-8")
    assert "__AUTOWORK_TOKEN__" in html
    assert "X-AutoWork-Token" in html


# ----------------------------------------------------- one resume, or several


def test_a_single_resume_is_a_complete_setup() -> None:
    """Most people have one. It must not feel like a degraded case."""
    config = profile_build.build(_answers())
    assert profile_build.validate(config) == []
    assert len(config["profiles"]) == 1
    assert next(iter(config["profiles"].values()))["preferred"] is True


def test_three_resumes_each_get_their_own_track() -> None:
    config = profile_build.build(_answers(resumes=[
        {"slug": s, "label": s.title(), "path": f"{s}.pdf", "roles": [r],
         "skills": [k], "text": k.lower()}
        for s, r, k in (("infra", "DevOps Engineer", "Terraform"),
                        ("product", "Full Stack Engineer", "Vue"),
                        ("agentic", "AI Engineer", "Python"))
    ]))
    assert list(config["profiles"]) == ["infra", "product", "agentic"]
    assert [p["preferred"] for p in config["profiles"].values()] == [True, False, False]


def test_every_configured_profile_resolves_to_a_real_resume() -> None:
    """The card names a file to send; that file has to exist."""
    config = rank.load_config()
    for slug, profile in config["profiles"].items():
        path = db.REPO_ROOT / profile["resume"]
        assert path.exists(), f"profile '{slug}' points at missing {profile['resume']}"


# --------------------------------------------------------- editing a profile


def test_config_round_trips_through_the_editor() -> None:
    """Opening settings and pressing save must change nothing on its own."""
    config = rank.load_config()
    back = profile_build.build(profile_build.to_answers(config))
    for slug, profile in config["profiles"].items():
        assert back["profiles"][slug]["skills"] == profile["skills"]
        assert back["profiles"][slug]["target_titles"] == profile["target_titles"]
    assert back["candidate"]["base"] == config["candidate"]["base"]
    assert profile_build.validate(back) == []


def test_editing_does_not_flatten_tuned_weights() -> None:
    """The resume text is never stored, so weights must be carried, not rederived.

    Without this, a saved profile whose skills were tuned by hand would be
    recomputed from an empty string — every skill equal — and the ranking would
    quietly change for the worse.
    """
    answers = profile_build.to_answers({
        "candidate": {"base": "Pune, India"},
        "constraints": {"location_tiers": {"remote": 8}},
        "profiles": {"main": {"label": "CV", "resume": "cv.pdf",
                              "skills": {"terraform": 10, "docker": 5},
                              "target_titles": {"platform engineer": 10}}},
    })
    built = profile_build.build(answers)
    assert built["profiles"]["main"]["skills"] == {"terraform": 10, "docker": 5}


def test_a_reuploaded_resume_is_reweighted() -> None:
    """Carrying weights must not stop a genuinely new upload from re-scoring."""
    answers = _answers()
    answers["resumes"][0]["text"] = "django django django python"
    answers["resumes"][0]["key_skills"] = ["Python"]
    built = profile_build.build(answers)
    assert built["profiles"]["main"]["skills"]["python"] == 10


def test_experience_band_is_recovered_from_stored_months() -> None:
    answers = profile_build.to_answers(
        {"candidate": {"experience_months_as_of": {"months": 30}}, "profiles": {}}
    )
    assert answers["experience_band"] == "2-4"
    assert answers["years"] == 2.5


def test_profile_editor_is_reachable_from_the_list() -> None:
    """Regression: the wizard only rendered when no profile existed, so once set
    up there was no way back to change a resume or move city."""
    from autowork import server

    html = (server.WEB / "index.html").read_text(encoding="utf-8")
    assert 'id="editprofile"' in html
    assert "openProfile" in html
    assert 'id="backtojobs"' in html


def test_arrow_keys_are_the_documented_movement() -> None:
    from autowork import server

    html = (server.WEB / "index.html").read_text(encoding="utf-8")
    hint = html.split('class="hint"')[1][:200]
    assert "↑" in hint and "↓" in hint, "arrows should be what the hint teaches"
    # j/k still work for anyone who reaches for them.
    assert 'case "ArrowDown": case "j"' in html


def test_full_width_rule_excludes_checkboxes() -> None:
    """Regression: `.field input { width:100% }` also matched the checkboxes.

    A stretched checkbox consumed its whole flex row and pushed its own label
    to the far edge of the form. Caught only by looking at the page — the two
    toggles rendered as a checkbox floating alone above text on the far right.
    """
    from autowork import server

    css = (server.WEB / "index.html").read_text(encoding="utf-8")
    assert ".field input:not([type=checkbox])" in css
    assert ".field input, .field select { width:100%; }" not in css


# ------------------------------------------------------------ role families


def _family_cfg(*names):
    import copy
    cfg = json.loads(profile_build.TEMPLATE.read_text(encoding="utf-8"))
    cfg = copy.deepcopy(cfg)
    cfg["constraints"]["role_families"] = list(names)
    return cfg


def test_a_non_engineer_is_not_gated_to_zero() -> None:
    """The whole point: a designer used to match nothing.

    Their title failed the engineering allowlist and "designer" was in the
    block list, so every posting was rejected twice over.
    """
    cfg = _family_cfg("design")
    assert rank.family_title_re(cfg).search("Senior Product Designer")
    assert "designer" not in rank.family_blocks(cfg)


def test_unpicked_families_become_the_blocklist() -> None:
    cfg = _family_cfg("engineering")
    blocks = rank.family_blocks(cfg)
    assert "account executive" in blocks and "marketing" in blocks


def test_blocks_are_intersected_across_chosen_families() -> None:
    """Union would block a job that sits squarely inside both choices.

    Someone open to engineering and data must still see "Data Engineer";
    engineering alone blocks "data analyst", and taking the union would keep
    that block even after data is chosen.
    """
    assert "data analyst" in rank.family_blocks(_family_cfg("engineering"))
    assert "data analyst" not in rank.family_blocks(_family_cfg("engineering", "data"))
    # Terms both families reject survive the intersection.
    assert "sales" in rank.family_blocks(_family_cfg("engineering", "data"))


def test_allowlist_is_the_union_of_chosen_families() -> None:
    both = _family_cfg("engineering", "design")
    assert rank.family_title_re(both).search("Backend Engineer")
    assert rank.family_title_re(both).search("UX Researcher")
    assert not rank.family_title_re(_family_cfg("engineering")).search("UX Researcher")


def test_any_family_disables_the_allowlist() -> None:
    """For fields the shipped list does not name, matching rests on titles."""
    assert rank.family_title_re(_family_cfg("any")) is None
    assert rank.family_title_re(_family_cfg("engineering", "any")) is None


def test_a_config_without_families_still_gates_as_engineering() -> None:
    """Backward compatibility: the original config predates all of this."""
    legacy = {"constraints": {"block_role_terms": ["sales"]}}
    assert rank.family_title_re(legacy).search("Software Engineer")
    assert not rank.family_title_re(legacy).search("Account Executive")
    assert rank.family_blocks(legacy) == ["sales"]


def test_family_of_tags_a_title_for_the_console_filter() -> None:
    cfg = _family_cfg("engineering", "design")
    assert rank.family_of("Staff Product Designer", cfg) == "design"
    assert rank.family_of("Backend Engineer", cfg) == "engineering"
    assert rank.family_of("Chief Financial Officer", cfg) is None


def test_every_family_declares_a_label_and_blocks() -> None:
    template = json.loads(profile_build.TEMPLATE.read_text(encoding="utf-8"))
    for name, family in template["role_families"].items():
        assert family.get("label"), f"{name} has no label for the picker"
        assert family.get("block_role_terms") is not None, f"{name} has no blocks"
        # Universal seniority terms belong to every family.
        assert "intern" in family["block_role_terms"], name


def test_wizard_infers_a_family_from_the_resume() -> None:
    designer = resume_parse.roles_in(
        "Product designer. Figma, wireframe, design system, UX designer."
    )
    assert resume_parse.families_for(designer) == ["design"]
    assert resume_parse.families_for(["Backend Engineer"]) == ["engineering"]
    # Never empty: an unrecognised resume still gets a workable default.
    assert resume_parse.families_for([]) == ["engineering"]


def test_families_survive_a_profile_round_trip() -> None:
    built = profile_build.build(_answers(families=["design", "product"]))
    assert built["constraints"]["role_families"] == ["design", "product"]
    assert profile_build.to_answers(built)["families"] == ["design", "product"]


def test_family_options_are_offered_to_the_wizard() -> None:
    keys = [f["key"] for f in profile_build.families()]
    assert "engineering" in keys and "any" in keys
    assert all(f["label"] for f in profile_build.families())


# ---------------------------------------------------------------- advisor


def test_advice_prompt_asks_for_argument_not_agreement() -> None:
    from autowork import tailor

    prompt = tailor.advice_prompt(
        [{"label": "CV", "text": "Terraform work", "skills": ["Terraform"],
          "roles": ["DevOps Engineer"], "key_skills": ["Terraform"]}],
        {"city": "Pune", "experience_band": "0-2", "families": ["engineering"]},
    )
    assert "do not just agree" in prompt
    assert "Pune" in prompt and "Terraform" in prompt
    # It may edit the config, but only after being told to.
    assert "only after I say yes" in prompt


def test_advice_prompt_handles_a_resume_not_reuploaded() -> None:
    """Editing an existing profile has no resume text to send."""
    from autowork import tailor

    prompt = tailor.advice_prompt(
        [{"label": "CV", "text": "", "skills": ["Go"], "roles": [], "key_skills": []}],
        {"city": "", "experience_band": "", "families": []},
    )
    assert "not re-uploaded" in prompt and "Go" in prompt


# ------------------------------------------------------- the macOS launcher


def test_launcher_does_not_type_into_a_login_shell_blindly() -> None:
    """Regression, observed live twice.

    `open -a Terminal <script>` starts a login shell and types the path into
    it. oh-my-zsh's "Would you like to update? [Y/n]" ate the leading `/`, so
    zsh received `Users/...` and failed. Replacing the fixed delay with a
    `busy` poll is what actually fixed it — a shell mid-update swallows the
    command too.
    """
    import inspect

    from autowork import tailor

    source = inspect.getsource(tailor._launch_macos)
    assert '"open", "-a", "Terminal"' not in source
    assert "busy of shellTab" in source, "must wait for the shell, not guess"
    # `tab` is an AppleScript class; using it as a variable is a syntax error.
    assert "set tab to" not in source


def test_launcher_reports_failure_rather_than_claiming_success() -> None:
    """Declining the automation prompt must not read as a launched terminal."""
    import inspect

    from autowork import tailor

    source = inspect.getsource(tailor._launch_macos)
    assert "returncode != 0" in source
    assert 'return False' in source


def test_applescript_quoting_survives_a_path_with_quotes() -> None:
    from autowork import tailor

    assert tailor._osa('a"b') == '"a\\"b"'
    assert tailor._osa("a\\b") == '"a\\\\b"'


# ------------------------------------------------------ windows and linux


def _win_script(tmp_path, **ctx_over):
    """Generate the script as it would be written on Windows."""
    import sys
    from unittest import mock

    from autowork import tailor

    ctx = {"title": "SDE-1", "company": "O'Brien Ltd", "resume": r"C:\Users\a\cv.md",
           "missing": ["Java"], "position": 3, **ctx_over}
    with mock.patch.object(sys, "platform", "win32"), \
         mock.patch.object(tailor, "TAILOR_DIR", tmp_path):
        prompt = tmp_path / "p.md"
        prompt.write_text("x")
        return tailor.runner_script(prompt, ctx, ctx_over.pop("tool", "claude"))


def test_windows_gets_powershell_not_a_shell_script(tmp_path) -> None:
    """Regression: `cmd /k run.sh` cannot execute `read`, `exec` or `$(cat ...)`.

    The Windows branch handed cmd a `#!/bin/sh` script and reported success
    regardless, so the page said a terminal had opened when nothing had run.
    """
    script = _win_script(tmp_path)
    assert script.suffix == ".ps1"
    body = script.read_text()
    for posix in ("#!/bin/sh", "read _", "exec ", "$(cat", "chmod"):
        assert posix not in body, f"POSIX construct {posix!r} leaked into PowerShell"
    assert "Read-Host" in body and "Write-Host" in body
    assert "Get-Content -Raw" in body


def test_powershell_doubles_embedded_quotes(tmp_path) -> None:
    """A company like O'Brien Ltd would otherwise end the string literal."""
    assert "'  O''Brien Ltd'" in _win_script(tmp_path).read_text()


def test_powershell_leaves_backslash_paths_alone(tmp_path) -> None:
    """Single-quoted PowerShell is literal — a Windows path must not be escaped."""
    assert r"C:\Users\a\cv.md" in _win_script(tmp_path).read_text()


def test_windows_still_stages_rather_than_running(tmp_path) -> None:
    body = _win_script(tmp_path).read_text()
    assert body.index("Read-Host") < body.index("claude ")


def test_windows_ollama_uses_the_cli(tmp_path) -> None:
    body = _win_script(tmp_path, tool="ollama").read_text()
    assert "autowork tailor 3 --ollama" in body


def test_windows_launcher_reports_a_missing_powershell() -> None:
    from unittest import mock

    from autowork import tailor

    with mock.patch("shutil.which", return_value=None):
        ok, detail = tailor._launch_windows("C:\\x.ps1")
    assert ok is False and "PowerShell" in detail


def test_windows_launcher_bypasses_execution_policy() -> None:
    """A locally generated script is unsigned; the default policy refuses it."""
    import inspect

    from autowork import tailor

    source = inspect.getsource(tailor._launch_windows)
    assert "Bypass" in source and "-NoExit" in source
    assert "cmd" not in source.split('"""')[2], "must not fall back to cmd"


def test_linux_launcher_reports_failure_when_nothing_is_installed() -> None:
    from unittest import mock

    from autowork import tailor

    with mock.patch("sys.platform", "linux"), mock.patch("shutil.which", return_value=None):
        ok, detail = tailor.launch_terminal(pathlib.Path("/tmp/x.sh"))
    assert ok is False and "terminal" in detail


def test_uploaded_resume_text_is_saved_for_tailoring(tmp_path, monkeypatch) -> None:
    """Regression: the wizard stored the upload's *filename* as the path.

    Nothing ever wrote that file, so `autowork tailor` failed with "no resume
    file" for anyone who set up through the wizard. It only worked for the
    original author, whose markdown resumes already existed on disk.
    """
    monkeypatch.setattr(profile_build, "RESUME_DIR", tmp_path)
    config = profile_build.build(_answers(resumes=[{
        "slug": "my-cv", "label": "My CV", "path": "cv.pdf",
        "roles": ["Backend Engineer"], "skills": ["Python"],
        "key_skills": ["Python"], "text": "Priya Sharma\nDjango and Python work.",
    }]))
    stored = config["profiles"]["my-cv"]["resume"]
    assert stored.endswith("resume-my-cv.md")
    assert "Django" in (tmp_path / "resume-my-cv.md").read_text()


def test_validate_flags_a_resume_path_that_does_not_exist() -> None:
    """The card tells you which file to send; a dangling path is a silent trap."""
    problems = profile_build.validate({
        "candidate": {"base": "Pune"},
        "profiles": {"x": {"resume": "nowhere.md",
                           "target_titles": {"a": 1}, "skills": {"b": 1}}},
    })
    assert any("nowhere.md" in p for p in problems)


def test_reset_clears_the_owner_but_keeps_the_shared_watchlist() -> None:
    """A clone must be usable by someone else, without re-probing 575 companies."""
    import inspect

    from autowork import cli

    source = inspect.getsource(cli.cmd_reset)
    # Only the deletion list matters; the "Kept:" message names the shared
    # files on purpose, which is what an earlier version of this test tripped on.
    deletes = source.split("personal = [", 1)[1].split("]", 1)[0]
    for name in ("profiles.json", "STATUS_JSON", "SEEN_TXT", "resume-*.md"):
        assert name in deletes, f"reset should clear {name}"
    for shared in ("boards.json", "companies.txt", "comp.json"):
        assert shared not in deletes, f"reset must not delete {shared}"


# ------------------------------------------------------- delisted postings


def _seed(conn, *rows):
    """rows: (id, token, source, last_seen)"""
    for job_id, token, source, seen in rows:
        conn.execute(
            """INSERT INTO jobs (id, dedup_key, source, company, company_token,
                                 title, url, first_seen, last_seen)
               VALUES (?,?,?,?,?,?,?,?,?)""",
            (job_id, job_id, source, "Acme", token, "SDE", "u", seen, seen),
        )
    conn.commit()


def test_a_posting_its_board_no_longer_lists_is_delisted(tmp_path) -> None:
    conn = db.connect(tmp_path / "t.db")
    _seed(conn,
          ("fresh", "acme", "greenhouse", "2026-08-12T00:00:00+00:00"),
          ("gone",  "acme", "greenhouse", "2026-08-07T00:00:00+00:00"))
    assert db.delisted_ids(conn) == {"gone"}


def test_a_board_outage_does_not_delist_everything_on_it(tmp_path) -> None:
    """The failure mode a fixed age check would have: if a board errors for
    days, none of its jobs were seen, and all of them look taken down."""
    conn = db.connect(tmp_path / "t.db")
    _seed(conn,
          ("a", "acme", "greenhouse", "2026-08-07T00:00:00+00:00"),
          ("b", "acme", "greenhouse", "2026-08-07T00:00:00+00:00"))
    assert db.delisted_ids(conn) == set()


def test_boards_are_judged_independently(tmp_path) -> None:
    """One company polling fine must not condemn another company's postings."""
    conn = db.connect(tmp_path / "t.db")
    _seed(conn,
          ("live", "acme",  "greenhouse", "2026-08-12T00:00:00+00:00"),
          ("old",  "other", "greenhouse", "2026-08-07T00:00:00+00:00"))
    assert db.delisted_ids(conn) == set()


def test_keyword_search_results_are_never_delisted(tmp_path) -> None:
    """A search returns what matched today; absence carries no information."""
    conn = db.connect(tmp_path / "t.db")
    _seed(conn,
          ("new", "li", "search", "2026-08-12T00:00:00+00:00"),
          ("old", "li", "search", "2026-08-01T00:00:00+00:00"))
    assert db.delisted_ids(conn) == set()


def test_same_day_repolls_are_within_the_grace_window(tmp_path) -> None:
    """Two polls hours apart must not delist whatever the second one missed."""
    conn = db.connect(tmp_path / "t.db")
    _seed(conn,
          ("a", "acme", "greenhouse", "2026-08-12T18:00:00+00:00"),
          ("b", "acme", "greenhouse", "2026-08-12T02:00:00+00:00"))
    assert db.delisted_ids(conn) == set()


def test_the_gate_rejects_a_delisted_posting() -> None:
    """Checked before anything about fit — none of that matters for a dead link."""
    conn = _corpus_or_skip()
    row = conn.execute("SELECT * FROM jobs LIMIT 1").fetchone()
    reason, _ = rank.gate_with_tier(row, rank.load_config(), delisted={row["id"]})
    assert reason and reason.startswith("delisted")


def test_the_delisted_set_reaches_the_score_rows_not_just_the_counter() -> None:
    """Regression: gating only run()'s counter printed "delisted 781" in the
    stats while every one of them stayed in the digest. The score rows are what
    the shortlist reads."""
    conn = _corpus_or_skip()
    row = conn.execute("SELECT * FROM jobs LIMIT 1").fetchone()
    score = rank.score_job(row, next(iter(rank.load_config()["profiles"])),
                           rank.load_config(), ats_only=False, delisted={row["id"]})
    assert score.passed is False
    assert score.gate and score.gate.startswith("delisted")
