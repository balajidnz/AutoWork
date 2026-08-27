"""Read the mailbox to find who to follow up with, and whether they replied.

The obvious design — put a "send follow-up" button next to each application —
does not survive contact with the data. Measured on seven real applications,
**none** had a usable recipient: job postings almost never carry a human's
address, and guessing `firstname.lastname@company.com` is what `contact.py`
exists to refuse.

The correct address is not in the posting. It is in the mailbox, in the
confirmation the company's ATS already sent. Reading it answers both questions
at once: who to write to, and whether they have already answered.

Deliberately narrow:

* **Read-only.** The mailbox is opened with `readonly=True`; nothing is
  deleted, moved, or even marked as read.
* **Headers first.** Only the envelope is fetched for matching. A body is
  pulled only for the one thread being replied to.
* **Same credential.** Gmail IMAP takes the app password already configured
  for sending, so there is no OAuth flow and no new secret.
"""

from __future__ import annotations

import email
import imaplib
import os
import re
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from email.header import decode_header, make_header

IMAP_HOST = "imap.gmail.com"

# Senders that are the ATS talking, not a person. A reply from one of these is
# an acknowledgement, not a conversation, and following up to it goes nowhere.
_AUTOMATED = re.compile(
    r"no-?reply|do-?not-?reply|donotreply|notification|mailer|automated"
    r"|@(greenhouse|lever|ashbyhq|myworkday|smartrecruiters|icims|workable)",
    re.I,
)

# Phrases an acknowledgement uses. Matched on the subject only: a human reply
# quoting the original would otherwise be misread as automated.
_ACK_SUBJECT = re.compile(
    r"thank you for (your )?(applying|application)|application (received|submitted)"
    r"|we('| ha)ve received|thanks for applying|your application to",
    re.I,
)


@dataclass(slots=True)
class Message:
    uid: str
    sender: str
    to: str
    subject: str
    date: str
    message_id: str
    references: str = ""

    @property
    def automated(self) -> bool:
        """An ATS acknowledgement rather than a person."""
        return bool(_AUTOMATED.search(self.sender) or _ACK_SUBJECT.search(self.subject))

    @property
    def when(self) -> datetime | None:
        try:
            return email.utils.parsedate_to_datetime(self.date)
        except (TypeError, ValueError):
            return None


@dataclass(slots=True)
class Conversation:
    """What the mailbox knows about one application."""

    company: str
    messages: list[Message] = field(default_factory=list)

    @property
    def human_replies(self) -> list[Message]:
        return [m for m in self.messages if not m.automated]

    @property
    def replied(self) -> bool:
        """Did a person write back? This is what the tracker actually wants."""
        return bool(self.human_replies)

    @property
    def reply_to(self) -> Message | None:
        """The message worth replying into.

        A human reply if there is one, otherwise the acknowledgement — which is
        still a real, correct, already-threaded address, and is the whole reason
        for reading the mailbox rather than guessing.
        """
        if self.human_replies:
            return self.human_replies[-1]
        return self.messages[-1] if self.messages else None


def credentials() -> tuple[str | None, str | None]:
    """The same pair used for sending. Gmail IMAP accepts the app password."""
    user = os.environ.get("SMTP_USER")
    password = os.environ.get("SMTP_PASS") or os.environ.get("SMTP_PASSWORD")
    return user, password


def connect(user: str | None = None, password: str | None = None):
    """A logged-in, read-only IMAP client."""
    user = user or credentials()[0]
    password = password or credentials()[1]
    if not (user and password):
        raise RuntimeError(
            "set SMTP_USER and SMTP_PASS in .env — Gmail IMAP takes the same "
            "app password you already use for sending"
        )
    client = imaplib.IMAP4_SSL(IMAP_HOST)
    client.login(user, password)
    # readonly: this reads a personal mailbox and must not change its state,
    # not even to mark a message as seen.
    client.select("INBOX", readonly=True)
    return client


def _decode(raw: str | None) -> str:
    if not raw:
        return ""
    try:
        return str(make_header(decode_header(raw)))
    except Exception:  # noqa: BLE001 — a malformed header must not stop the scan
        return raw


def _parse(uid: str, blob: bytes) -> Message:
    msg = email.message_from_bytes(blob)
    return Message(
        uid=uid,
        sender=_decode(msg.get("From")),
        to=_decode(msg.get("To")),
        subject=_decode(msg.get("Subject")),
        date=msg.get("Date") or "",
        message_id=msg.get("Message-ID") or "",
        references=msg.get("References") or "",
    )


def search(client, company: str, domain: str | None = None,
           since_days: int = 120) -> list[Message]:
    """Messages that mention a company, newest last.

    Two searches rather than one: the company's own domain finds the ATS
    acknowledgement, and the plain name finds anything sent from a recruiter's
    personal or agency address.
    """
    since = (datetime.now(UTC) - timedelta(days=since_days)).strftime("%d-%b-%Y")
    uids: list[str] = []
    terms = [f'(SINCE {since} FROM "{domain}")'] if domain else []
    terms.append(f'(SINCE {since} TEXT "{company}")')
    for term in terms:
        try:
            status, data = client.search(None, term)
        except Exception:  # noqa: BLE001 — one bad term must not lose the other
            continue
        if status == "OK" and data and data[0]:
            uids.extend(data[0].split())

    seen: dict[str, Message] = {}
    for uid in uids:
        key = uid.decode() if isinstance(uid, bytes) else str(uid)
        if key in seen:
            continue
        status, data = client.fetch(key, "(BODY.PEEK[HEADER])")
        if status != "OK" or not data or not data[0]:
            continue
        blob = data[0][1] if isinstance(data[0], tuple) else data[0]
        if isinstance(blob, bytes):
            seen[key] = _parse(key, blob)
    return sorted(seen.values(), key=lambda m: m.when or datetime.min.replace(tzinfo=UTC))


def conversation(client, company: str, domain: str | None = None) -> Conversation:
    return Conversation(company=company, messages=search(client, company, domain))
