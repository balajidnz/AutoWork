"""Read a resume and guess what its owner is looking for.

Deliberately a *guess*. Everything here is shown back for correction before it
is used — the setup flow presents these fields editable, because a parser that
silently mis-reads a resume produces a shortlist that is quietly wrong for
weeks, and the person has no way to tell.

Skill detection reuses `coverage.VOCABULARY` rather than defining its own list.
The same vocabulary decides "your resume evidences Terraform" on a job card, so
if the two disagreed a role could ask for a skill you have and still be scored
as a gap.
"""

from __future__ import annotations

import io
import re
from dataclasses import dataclass, field

from autowork import coverage

# Titles worth targeting, and the phrasings that imply them. Ordered: the first
# match wins the "primary role" slot in the wizard.
_ROLE_HINTS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("Platform Engineer", ("platform engineer", "platform engineering")),
    ("DevOps Engineer", ("devops", "dev ops", "sre", "site reliability")),
    ("Infrastructure Engineer", ("infrastructure engineer", "cloud engineer")),
    ("Full Stack Engineer", ("full stack", "fullstack", "full-stack")),
    ("Backend Engineer", ("backend", "back-end", "back end", "server-side")),
    ("Frontend Engineer", ("frontend", "front-end", "front end")),
    ("Data Engineer", ("data engineer", "data pipeline", "etl")),
    ("Machine Learning Engineer", ("machine learning", "ml engineer", "deep learning")),
    ("AI Engineer", ("llm", "generative ai", "genai", "ai engineer", "agentic")),
    ("Mobile Engineer", ("android developer", "ios developer", "react native", "flutter")),
    ("Security Engineer", ("security engineer", "appsec", "penetration testing")),
    ("QA Engineer", ("qa engineer", "test automation", "quality assurance")),
    ("Software Engineer", ("software engineer", "software development", "sde", "swe")),
    # Non-technical. Without these a designer's or marketer's resume yields no
    # roles at all, and the wizard opens on an empty form.
    ("Product Manager", ("product manager", "product owner", "roadmap", "prd")),
    ("Product Designer", ("product designer", "ux designer", "ui designer",
                          "figma", "wireframe", "design system")),
    ("Data Analyst", ("data analyst", "dashboard", "tableau", "power bi", "excel model")),
    ("Marketing Manager", ("marketing", "campaign", "seo", "content strategy", "brand")),
    # "pipeline" alone is not a sales signal: data pipelines, CI pipelines and
    # API pipelines all appear on engineering resumes, and one backend CV was
    # classified into the sales family because of it.
    ("Account Executive", ("account executive", "sales quota", "sales pipeline",
                           "b2b sales", "customer success", "closed won")),
    ("Operations Manager", ("operations manager", "process improvement",
                            "supply chain", "vendor management")),
    ("Recruiter", ("recruiter", "talent acquisition", "sourcing candidates")),
)

# Which family each detected role implies, so the wizard can preselect one.
ROLE_FAMILY = {
    "Platform Engineer": "engineering", "DevOps Engineer": "engineering",
    "Infrastructure Engineer": "engineering", "Full Stack Engineer": "engineering",
    "Backend Engineer": "engineering", "Frontend Engineer": "engineering",
    "Mobile Engineer": "engineering", "Security Engineer": "engineering",
    "QA Engineer": "engineering", "Software Engineer": "engineering",
    "AI Engineer": "engineering",
    "Data Engineer": "data", "Machine Learning Engineer": "data",
    "Data Analyst": "data",
    "Product Manager": "product", "Product Designer": "design",
    "Marketing Manager": "marketing", "Account Executive": "sales",
    "Operations Manager": "operations", "Recruiter": "operations",
}


def families_for(roles: list[str]) -> list[str]:
    """Families implied by the detected roles, most-evidenced first."""
    seen: dict[str, int] = {}
    for role in roles:
        family = ROLE_FAMILY.get(role)
        if family:
            seen[family] = seen.get(family, 0) + 1
    return sorted(seen, key=lambda f: -seen[f]) or ["engineering"]

# Tie-break by the order above, which runs specific -> generic: on an equal
# count, "Platform Engineer" is a more useful target title than "Software
# Engineer", and dict order alone would not guarantee that.
_ROLE_ORDER = {title: i for i, (title, _) in enumerate(_ROLE_HINTS)}

# "2 years", "2+ yrs", "2.5 years of experience"
_YEARS = re.compile(r"(\d+(?:\.\d+)?)\s*\+?\s*(?:years?|yrs?)", re.I)
# Date ranges on an experience line: "Jan 2024 - Present", "2022 – 2024"
_MONTHS = ("jan", "feb", "mar", "apr", "may", "jun",
           "jul", "aug", "sep", "oct", "nov", "dec")
_RANGE = re.compile(
    rf"(?:({'|'.join(_MONTHS)})\w*\s+)?(20\d\d)\s*(?:[-–—]+|\bto\b)\s*"
    rf"(?:(?:({'|'.join(_MONTHS)})\w*\s+)?(20\d\d)|present|current|now)",
    re.I,
)
_EMAIL = re.compile(r"[\w.+-]+@[\w-]+\.[\w.-]+")
_PHONE = re.compile(r"(?:\+91[\s-]?)?\b[6-9]\d{9}\b")
_LPA = re.compile(r"(\d+(?:\.\d+)?)\s*(?:lpa|lakhs?\s*(?:per\s*annum)?|l\.p\.a)", re.I)

_INDIAN_CITIES = (
    "Bengaluru", "Bangalore", "Hyderabad", "Pune", "Chennai", "Mumbai",
    "Delhi", "Gurgaon", "Gurugram", "Noida", "Kolkata", "Ahmedabad",
    "Jaipur", "Kochi", "Coimbatore", "Indore", "Chandigarh",
)


@dataclass(slots=True)
class Parsed:
    """What the resume appears to say. Every field is a suggestion."""

    name: str = ""
    email: str = ""
    location: str = ""
    skills: list[str] = field(default_factory=list)
    roles: list[str] = field(default_factory=list)
    years: float | None = None
    current_ctc_lpa: float | None = None
    text: str = ""

    @property
    def confident(self) -> bool:
        """Enough signal to be worth showing, rather than an empty form."""
        return bool(self.skills) and (self.years is not None or bool(self.roles))


def extract_text(data: bytes, filename: str = "") -> str:
    """Text from a PDF, or a plain-text/markdown resume passed straight through."""
    if filename.lower().endswith((".txt", ".md")) or not data[:5].startswith(b"%PDF"):
        return data.decode("utf-8", errors="replace")

    from pypdf import PdfReader

    reader = PdfReader(io.BytesIO(data))
    return "\n".join(page.extract_text() or "" for page in reader.pages)


def skills_in(text: str) -> list[str]:
    """Canonical vocabulary terms the resume evidences.

    `coverage._present` already handles the alias problem (K8s vs Kubernetes,
    GHA vs GitHub Actions) and is covered by its own tests.
    """
    lowered = text.lower()
    return [
        term for term, variants in coverage.VOCABULARY.items()
        if coverage._present(term, variants, lowered)
    ]


def roles_in(text: str, limit: int = 3) -> list[str]:
    """The roles the resume argues for hardest.

    Any mention counts as a match, so a full-stack resume that once says
    "collaborated with the ML team" matches Machine Learning Engineer just as
    strongly as Full Stack Engineer. Ranking by how often the phrasing recurs
    separates what someone does from what they merely touched, and the cap
    keeps the wizard from opening with seven target titles to delete.
    """
    lowered = text.lower()
    scored = []
    for title, hints in _ROLE_HINTS:
        hits = sum(lowered.count(hint) for hint in hints)
        if hits:
            scored.append((hits, title))
    scored.sort(key=lambda pair: (-pair[0], _ROLE_ORDER[pair[1]]))
    return [title for _, title in scored[:limit]]


def years_in(text: str) -> float | None:
    """Total experience, preferring stated dates over a stated number.

    A resume saying "3+ years of experience with Kubernetes" is describing the
    technology, not the person, so an explicit claim is only trusted when no
    employment date range is available to measure instead.
    """
    if spans := _date_span_months(text):
        return round(spans / 12, 1)
    if match := _YEARS.search(text):
        return float(match.group(1))
    return None


def _date_span_months(text: str) -> int:
    """Months covered by employment date ranges, merging overlaps.

    Overlapping ranges are common — a promotion is often listed as two rows at
    the same company — and adding them up double-counts the same time.
    """
    spans: list[tuple[int, int]] = []
    today = _today_index()
    for match in _RANGE.finditer(text):
        start_month, start_year, end_month, end_year = match.groups()
        start = int(start_year) * 12 + _month_index(start_month)
        end = today if end_year is None else int(end_year) * 12 + _month_index(end_month)
        if start <= end <= today + 1:
            spans.append((start, end))
    if not spans:
        return 0

    # Sort once and seed from the sorted head. Seeding from spans[0] (document
    # order) while iterating sorted(spans)[1:] silently drops the earliest job:
    # a resume listing the current role first read 15 months instead of 21.
    spans.sort()
    total, current_start, current_end = 0, *spans[0]
    for start, end in spans[1:]:
        if start <= current_end:
            current_end = max(current_end, end)
        else:
            total += current_end - current_start
            current_start, current_end = start, end
    return total + current_end - current_start


def _month_index(name: str | None) -> int:
    if not name:
        return 0
    return _MONTHS.index(name[:3].lower()) if name[:3].lower() in _MONTHS else 0


def _today_index() -> int:
    from datetime import UTC, datetime

    now = datetime.now(UTC)
    return now.year * 12 + now.month - 1


def _name_in(text: str) -> str:
    """The first line that looks like a person's name.

    Resumes lead with it, in larger type that the extractor flattens away, so
    position is the only signal left. Anything with an @ or a digit is a
    contact line, not a name.
    """
    for line in text.splitlines()[:6]:
        # Markdown resumes lead with "# Name — note"; PDFs lead with the name
        # alone. Strip the heading marker and anything after a dash or pipe,
        # which is a subtitle rather than part of the name.
        line = re.sub(r"^#+\s*", "", line.strip())
        line = re.split(r"\s+[—–|]\s+|\s+-\s+", line)[0].strip()
        if not (2 <= len(line.split()) <= 4) or len(line) > 48:
            continue
        if any(c.isdigit() for c in line) or "@" in line or "|" in line:
            continue
        if re.fullmatch(r"[A-Za-z][A-Za-z.'\- ]+", line):
            return line.title() if line.isupper() else line
    return ""


def _location_in(text: str) -> str:
    lowered = text.lower()
    for city in _INDIAN_CITIES:
        if re.search(rf"(?<![a-z]){city.lower()}(?![a-z])", lowered):
            return f"{city}, India"
    return ""


def parse(data: bytes, filename: str = "") -> Parsed:
    text = extract_text(data, filename)
    ctc = _LPA.search(text)
    return Parsed(
        name=_name_in(text),
        email=(m.group(0) if (m := _EMAIL.search(text)) else ""),
        location=_location_in(text),
        skills=skills_in(text),
        roles=roles_in(text),
        years=years_in(text),
        current_ctc_lpa=float(ctc.group(1)) if ctc else None,
        text=text,
    )
