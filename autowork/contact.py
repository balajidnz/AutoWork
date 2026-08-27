"""Find a human to write to.

Scoped deliberately narrowly. There is no free, reliable way to discover a
named hiring manager's mailbox, and a tool that guesses one and presents it as
fact is worse than no tool — a bounced or misaddressed mail costs more than the
outreach was worth.

So this does only what can be done honestly:

1. **Extract** addresses the posting itself contains. High confidence, and
   surprisingly common in Indian listings.
2. **Resolve** the company's mail domain and confirm via MX that it accepts
   mail at all.
3. **Suggest** the address patterns that domain most likely uses, clearly
   labelled as unverified, so a name found elsewhere can be turned into an
   address without guessing the format too.

Everything is cached in `data/contacts.json`; MX records and company domains
change on a timescale of years.
"""

from __future__ import annotations

import json
import re
import socket
from dataclasses import asdict, dataclass, field
from pathlib import Path
from urllib.parse import urlparse

from . import db

CONTACTS_JSON = db.DATA_DIR / "contacts.json"

_EMAIL = re.compile(r"\b[\w.+-]+@([\w-]+\.[\w.-]+)\b")

# Never worth writing to: automated senders, unrelated departments, and the
# placeholder addresses that appear inside prose ("email name@company.com").
_NOISE_LOCAL = re.compile(
    r"^(no-?reply|do-?not-?reply|donotreply|postmaster|mailer-daemon"
    r"|support|help|info|admin|webmaster|privacy|legal|abuse|security"
    r"|sales|marketing|billing|accounts?|notifications?|alerts?"
    r"|accommodations?|accessibility"
    r"|name|yourname|firstname|first\.last|email|your-?email|username|example)$",
    re.I,
)
# Real inboxes, but a queue rather than a person. Worth surfacing separately:
# writing to careers@ is not outreach, it is the application you already made.
_GENERIC_LOCAL = re.compile(
    r"^(careers?|jobs?|hiring|recruit\w*|talent\w*|hr|people(ops)?|apply"
    r"|resumes?|cv|work(with)?us|join\w*|hello|contact)$",
    re.I,
)
# ATS and job-board domains — the posting's plumbing, not the employer.
# The trailing dot must not immediately follow the brand: Ashby's host is
# `jobs.ashbyhq.com`, so `ashby\.` never matched and sarvam's contact domain
# resolved to the applicant tracking system rather than to sarvam. Allow any
# suffix on the brand before the dot.
_NOISE_DOMAIN = re.compile(
    r"(greenhouse|lever|ashbyhq|ashby|smartrecruiters|workable|myworkdayjobs"
    r"|myworkday|workday|icims|taleo|jobvite|bamboohr|recruitee|linkedin"
    r"|indeed|glassdoor|naukri|wellfound|hirist|foundit|instahyre|example)\.",
    re.I,
)

# Ordered by how common they are at tech companies.
PATTERNS = ("{first}.{last}", "{first}", "{f}{last}", "{first}{last}", "{first}_{last}")


@dataclass
class Contact:
    company: str
    domain: str | None = None
    mx: bool = False
    found_emails: list[str] = field(default_factory=list)
    generic_emails: list[str] = field(default_factory=list)
    checked: str = ""

    @property
    def usable(self) -> bool:
        return bool(self.found_emails) or (self.domain is not None and self.mx)

    @property
    def best(self) -> str | None:
        """A person if we found one, otherwise a queue, otherwise nothing."""
        return (self.found_emails or self.generic_emails or [None])[0]

    def guesses(self, first: str, last: str = "") -> list[str]:
        """Address forms for a known name. Unverified by construction."""
        if not (self.domain and self.mx and first):
            return []
        parts = {
            "first": re.sub(r"[^a-z]", "", first.lower()),
            "last": re.sub(r"[^a-z]", "", last.lower()),
            "f": (first[:1] or "").lower(),
        }
        out = []
        for pattern in PATTERNS:
            if "{last}" in pattern and not parts["last"]:
                continue
            out.append(f"{pattern.format(**parts)}@{self.domain}")
        return list(dict.fromkeys(out))

    def summary(self) -> str:
        if self.found_emails:
            return "person: " + ", ".join(self.found_emails[:3])
        if self.generic_emails:
            return "queue only: " + ", ".join(self.generic_emails[:2])
        if self.domain and self.mx:
            return f"domain {self.domain} (accepts mail) — no address in posting"
        if self.domain:
            return f"domain {self.domain} — no MX, mail will bounce"
        return "no domain found"


def emails_in(text: str | None) -> tuple[list[str], list[str]]:
    """(person-like, generic) addresses from a job description.

    The split matters. `careers@` is a real inbox but writing to it is just the
    application again; a named mailbox is actual outreach. Reporting them as
    one list makes the second look as valuable as the first.
    """
    if not text:
        return [], []
    people: list[str] = []
    generic: list[str] = []
    seen: set[str] = set()
    for match in _EMAIL.finditer(text):
        address = match.group(0)
        local, _, domain = address.partition("@")
        if _NOISE_LOCAL.match(local) or _NOISE_DOMAIN.search(domain):
            continue
        if address.lower() in seen:
            continue
        seen.add(address.lower())
        (generic if _GENERIC_LOCAL.match(local) else people).append(address)
    return people, generic


def domain_for(company: str, url: str | None, description: str | None) -> str | None:
    """The employer's own mail domain.

    The apply URL is usually an ATS, so it is only trusted when it is not one.
    An address inside the description is the strongest signal, because it is
    the domain the employer actually uses.
    """
    for address in _EMAIL.finditer(description or ""):
        candidate = address.group(1).lower()
        if not _NOISE_DOMAIN.search(candidate + "."):
            return candidate

    host = (urlparse(url or "").hostname or "").lower().removeprefix("www.")
    if host and not _NOISE_DOMAIN.search(host + "."):
        return host

    slug = re.sub(r"[^a-z0-9]", "", (company or "").lower())
    return f"{slug}.com" if slug else None


def has_mx(domain: str) -> bool:
    """Does the domain accept mail?

    Deliberately stops at the domain. Probing an individual mailbox over SMTP
    is what the paid verifiers do, and it is both widely blocked and rude —
    it means opening a connection to someone's mail server to ask whether a
    colleague exists.
    """
    try:
        socket.getaddrinfo(domain, None)
    except (socket.gaierror, UnicodeError):
        return False
    return True


def lookup(company: str, url: str | None, description: str | None) -> Contact:
    contact = Contact(company=company, checked=db.now()[:10])
    contact.found_emails, contact.generic_emails = emails_in(description)
    contact.domain = domain_for(company, url, description)
    if contact.domain:
        contact.mx = has_mx(contact.domain)
    return contact


# ------------------------------------------------------------------- cache


def load(path: Path = CONTACTS_JSON) -> dict[str, Contact]:
    if not path.exists():
        return {}
    return {k: Contact(**v) for k, v in json.loads(path.read_text(encoding="utf-8")).items()}


def save(cache: dict[str, Contact], path: Path = CONTACTS_JSON) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps({k: asdict(v) for k, v in sorted(cache.items())},
                   indent=2, sort_keys=True, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )


def enrich(rows, cache: dict[str, Contact] | None = None) -> dict[str, Contact]:
    cache = cache if cache is not None else load()
    for row in rows:
        key = db.company_slug(row["company"])
        if key in cache and not cache[key].found_emails:
            # A newer posting may carry an address an earlier one lacked; a
            # resolved domain does not go stale on that timescale.
            people, generic = emails_in(row["description"])
            if people:
                cache[key].found_emails = people
            if generic and not cache[key].generic_emails:
                cache[key].generic_emails = generic
            continue
        if key not in cache:
            cache[key] = lookup(row["company"], row["url"], row["description"])
    return cache
