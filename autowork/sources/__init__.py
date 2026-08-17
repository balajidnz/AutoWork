"""Source adapters.

Every adapter exposes ``fetch(client, token) -> list[Job]`` and raises
``BoardNotFound`` when the token does not correspond to a real board, which is
what the watchlist verifier keys off.
"""

from __future__ import annotations

import html
import re
from datetime import UTC, datetime
from html.parser import HTMLParser


class BoardNotFound(Exception):
    """The ATS returned a definitive 'no such board' for this token."""


class _TextExtractor(HTMLParser):
    _SKIP = {"script", "style"}
    _BREAK = {"p", "div", "br", "li", "h1", "h2", "h3", "h4", "h5", "h6", "tr"}

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self._skipping = 0

    def handle_starttag(self, tag: str, attrs) -> None:
        if tag in self._SKIP:
            self._skipping += 1
        elif tag in self._BREAK:
            self.parts.append("\n")

    def handle_endtag(self, tag: str) -> None:
        if tag in self._SKIP and self._skipping:
            self._skipping -= 1
        elif tag in self._BREAK:
            self.parts.append("\n")

    def handle_data(self, data: str) -> None:
        if not self._skipping:
            self.parts.append(data)


def html_to_text(raw: str | None) -> str | None:
    """Flatten a job description to plain text.

    Greenhouse returns entity-encoded HTML (``&lt;p&gt;``), so unescape first
    and let the parser's own charref handling deal with the second layer.
    """
    if not raw:
        return None
    parser = _TextExtractor()
    parser.feed(html.unescape(raw))
    parser.close()
    text = "".join(parser.parts)
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n\s*\n\s*\n+", "\n\n", text)
    return text.strip() or None


_REMOTE_RE = re.compile(r"\bremote\b|\bwork from home\b|\banywhere\b", re.I)


def looks_remote(*values: str | None) -> bool:
    return any(_REMOTE_RE.search(v) for v in values if v)


def iso(value) -> str | None:
    """Normalise the three date encodings these APIs use to ISO-8601 UTC."""
    if value in (None, "", 0):
        return None
    try:
        if isinstance(value, (int, float)):  # Lever: epoch milliseconds
            return datetime.fromtimestamp(value / 1000, UTC).isoformat(timespec="seconds")
        text = str(value).replace("Z", "+00:00")
        parsed = datetime.fromisoformat(text)
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=UTC)
        return parsed.astimezone(UTC).isoformat(timespec="seconds")
    except (ValueError, OSError, OverflowError):
        return None


_MONEY = re.compile(
    r"(?P<ccy>[$₹€£]|USD|INR|EUR|GBP)?\s*(?P<num>\d[\d,.]*)\s*(?P<mult>[KkLlMm]|Cr|lakh|crore)?",
    re.I,
)
_MULT = {"k": 1_000, "l": 100_000, "m": 1_000_000, "cr": 10_000_000,
         "lakh": 100_000, "crore": 10_000_000}
_CCY = {"$": "USD", "₹": "INR", "€": "EUR", "£": "GBP"}


def parse_salary(text: str | None) -> tuple[float | None, float | None, str | None]:
    """Best-effort range parse of strings like '$257K – $335K' or '₹20L - ₹40L'.

    Returns (min, max, currency); any element may be None. This is a ranking
    signal, not an accounting figure — a wrong parse costs a few score points,
    so a loose regex is the right tradeoff against a currency library.
    """
    if not text:
        return None, None, None
    amounts: list[float] = []
    currency: str | None = None
    for m in _MONEY.finditer(text):
        raw_num = m.group("num")
        if not raw_num or not any(c.isdigit() for c in raw_num):
            continue
        try:
            value = float(raw_num.replace(",", ""))
        except ValueError:
            continue
        mult = (m.group("mult") or "").lower()
        value *= _MULT.get(mult, 1)
        if value < 1000:  # bare "2" from "2 years experience" etc.
            continue
        ccy = m.group("ccy")
        if ccy and currency is None:
            currency = _CCY.get(ccy, ccy.upper())
        amounts.append(value)
    if not amounts:
        return None, None, None
    return min(amounts), (max(amounts) if len(amounts) > 1 else None), currency


# Sources that grade seniority themselves use their own vocabularies —
# SmartRecruiters says `mid_senior_level`, LinkedIn says `mid-senior level`.
# Normalising both means the gate can trust the source instead of guessing
# from the title.
_LEVELS = {
    "internship": "internship", "intern": "internship",
    "entry_level": "entry", "entry level": "entry", "entry": "entry",
    "associate": "associate",
    "mid_senior_level": "mid_senior", "mid-senior level": "mid_senior",
    "mid senior level": "mid_senior", "midsenior": "mid_senior",
    "director": "director", "executive": "executive",
}


def normalise_level(value: str | None) -> str | None:
    """Map a source's seniority label onto our vocabulary.

    Returns None for anything unrecognised — including SmartRecruiters'
    `not_applicable`, which is an absence of information rather than a level,
    and must not be mistaken for one.
    """
    if not value:
        return None
    return _LEVELS.get(str(value).strip().lower().replace("_", " ").replace("-", " ")) \
        or _LEVELS.get(str(value).strip().lower())
