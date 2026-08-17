"""Resume markdown parsing and PDF rendering."""

from __future__ import annotations

import pytest

from autowork import resume

# A made-up person. This file is public, and a real CV in a test fixture
# publishes an employer and a set of achievements to everyone who clones it.
# Every character that matters to a test is still here: the em-dash suffix, the
# blockquote, a hard-wrapped bullet and paragraph, inline bold, and a role
# heading with a trailing date.
SAMPLE = """# Priya Sharma — infra base
Pune, India · a@b.com · +91 0000

> Guidance for the tailoring model, not resume content.

## Summary
Full-stack engineer with 2 years at Northwind, converted from intern to SDE-1,
shipping product across the stack.

## Northwind — Full-Stack Engineer (SDE-1), May 2025 – present

### Cloud infrastructure
- Cut org-wide cloud spend **41%**, from $53.4K to $31.6K/month, and
  migrated ~3.26M records across 4 databases.
- Owned production incident response.

## Skills
- **Languages:** Go, Ruby
"""


@pytest.fixture(scope="module")
def blocks():
    return resume.parse(SAMPLE)


def kinds(blocks):
    return [b.kind for b in blocks]


def test_name_drops_the_variant_suffix():
    assert resume.parse("# Priya Sharma — infra base")[0].text == "Priya Sharma"


def test_contact_line_follows_the_name(blocks):
    assert blocks[1].kind == "contact"
    assert "a@b.com" in blocks[1].text


def test_blockquote_guidance_is_not_content(blocks):
    assert not any("Guidance for the tailoring" in b.text for b in blocks)


def test_short_heading_is_a_section(blocks):
    assert any(b.kind == "section" and b.text == "Summary" for b in blocks)


def test_role_heading_splits_off_the_date(blocks):
    role = next(b for b in blocks if b.kind == "role")
    assert role.text == "Northwind — Full-Stack Engineer (SDE-1)"
    assert role.right == "May 2025 – present"


def test_hard_wrapped_source_lines_join_their_block(blocks):
    """The resumes are wrapped at ~80 columns. Without lazy continuation each
    source line became its own paragraph, losing the hanging indent."""
    bullet = next(b for b in blocks if b.text.startswith("Cut org-wide"))
    assert "migrated ~3.26M records across 4 databases." in bullet.text
    assert bullet.kind == "bullet"
    # and the continuation must not have produced a stray block
    assert not any(b.text.startswith("migrated ~3.26M") for b in blocks)


def test_summary_paragraph_also_joins(blocks):
    para = next(b for b in blocks if b.text.startswith("Full-stack engineer"))
    assert para.text.endswith("shipping product across the stack.")


# --------------------------------------------------------------- inline bold


@pytest.mark.parametrize(
    "text,expected",
    [
        ("plain", [("plain", False)]),
        ("cut **41%** today", [("cut ", False), ("41%", True), (" today", False)]),
        ("**Languages:** Go", [("Languages:", True), (" Go", False)]),
    ],
)
def test_runs(text, expected):
    assert resume.runs(text) == expected


# ------------------------------------------------------------ transliteration


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("Northwind — Engineer", "Northwind - Engineer"),   # no doubled spaces
        ("2025 – present", "2025 - present"),
        ("₹22L", "INR 22L"),
        ("don’t", "don't"),
        ("a · b", "a · b"),                                # already latin-1
    ],
)
def test_latin1_transliteration(raw, expected):
    """fpdf2's core fonts raise on these rather than degrading."""
    assert resume.latin1(raw) == expected


def test_latin1_output_is_encodable():
    assert resume.latin1("— – ₹ ’ • … →").encode("latin-1")


# -------------------------------------------------------------------- render


def test_renders_a_single_page(tmp_path):
    out = resume.render(resume.parse(SAMPLE), tmp_path / "r.pdf")
    assert out.exists() and out.stat().st_size > 1000
    assert resume.pages(resume.parse(SAMPLE)) == 1


def test_autofit_shrinks_until_one_page(tmp_path):
    """A tailored resume varies in length; two pages at SDE-1 reads worse than
    one tight page."""
    long = SAMPLE + "\n".join(f"- Bullet number {i} with plenty of text to wrap onto "
                              f"several lines and force a second page." for i in range(90))
    src = tmp_path / "long.md"
    src.write_text(long, encoding="utf-8")
    blocks = resume.parse(long)
    assert resume.pages(blocks, body_pt=9.0) > 1     # overflows at full size
    resume.render_file(src, tmp_path / "fit.pdf")    # …and fitting rescues it
    assert (tmp_path / "fit.pdf").exists()
