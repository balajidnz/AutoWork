"""Reading the mailbox to find who to follow up with.

None of this can be tested against a real Gmail account from here — the
credentials live in a gitignored `.env` and in GitHub secrets. So the IMAP
client is injected, and these tests cover the parts that decide behaviour: what
counts as a human reply, which message is worth replying into, and that the
mailbox is never opened for writing.
"""

from __future__ import annotations

import email.utils
import inspect
from datetime import UTC, datetime, timedelta

import pytest

from autowork import inbox


def _raw(sender: str, subject: str, days_ago: int = 0, msg_id: str = "<x@y>") -> bytes:
    when = email.utils.format_datetime(datetime.now(UTC) - timedelta(days=days_ago))
    return (f"From: {sender}\r\nTo: me@example.com\r\nSubject: {subject}\r\n"
            f"Date: {when}\r\nMessage-ID: {msg_id}\r\n\r\n").encode()


class FakeIMAP:
    """Just enough IMAP to exercise the search path."""

    def __init__(self, messages: dict[str, bytes]):
        self.messages = messages
        self.selected_readonly: bool | None = None

    def select(self, mailbox, readonly=False):
        self.selected_readonly = readonly
        return "OK", [b""]

    def search(self, charset, term):
        return "OK", [b" ".join(k.encode() for k in self.messages)]

    def fetch(self, uid, spec):
        return "OK", [(b"1", self.messages[uid])]

    def logout(self):
        return "BYE", [b""]


# ------------------------------------------------------- what counts as a reply


@pytest.mark.parametrize("sender,subject", [
    ("no-reply@greenhouse.io", "Thank you for applying to Swiggy"),
    ("donotreply@ashbyhq.com", "Application received"),
    ("notifications@lever.co", "Your application to sarvam"),
    ("Careers <careers@acme.com>", "Thanks for applying"),
])
def test_acknowledgements_are_not_replies(sender, subject):
    """Following up to a no-reply address goes nowhere, and counting it as a
    reply would tell you a company had answered when nobody had."""
    assert inbox.Message("1", sender, "me", subject, "", "<a>").automated


@pytest.mark.parametrize("sender,subject", [
    ("Priya Sharma <priya@swiggy.com>", "Re: your application"),
    ("recruiting@sarvam.ai", "Next steps — quick call?"),
])
def test_a_person_writing_back_is_a_reply(sender, subject):
    assert not inbox.Message("1", sender, "me", subject, "", "<a>").automated


def test_a_human_quoting_the_acknowledgement_is_still_a_human():
    """The ack phrases are matched on the subject only — a person replying to
    the confirmation would otherwise be misread as automated."""
    m = inbox.Message("1", "Priya <priya@swiggy.com>", "me",
                      "Re: Next steps", "", "<a>")
    assert not m.automated


# --------------------------------------------------------- which thread to use


def test_replies_into_the_human_thread_when_there_is_one():
    ack = inbox.Message("1", "no-reply@greenhouse.io", "me", "Thank you for applying", "", "<a>")
    human = inbox.Message("2", "Priya <priya@swiggy.com>", "me", "Re: hello", "", "<b>")
    convo = inbox.Conversation("Swiggy", [ack, human])
    assert convo.replied
    assert convo.reply_to is human


def test_falls_back_to_the_acknowledgement():
    """This is the whole point of reading the mailbox: even the automated
    confirmation is a real, correct, already-threaded address, where the job
    posting gave none at all."""
    ack = inbox.Message("1", "no-reply@greenhouse.io", "me", "Thank you for applying", "", "<a>")
    convo = inbox.Conversation("Swiggy", [ack])
    assert not convo.replied
    assert convo.reply_to is ack


def test_no_messages_means_no_recipient():
    convo = inbox.Conversation("Swiggy", [])
    assert convo.reply_to is None and not convo.replied


# ------------------------------------------------------------------- searching


def test_search_returns_messages_oldest_first():
    client = FakeIMAP({
        "1": _raw("no-reply@greenhouse.io", "Thank you for applying", days_ago=20, msg_id="<a>"),
        "2": _raw("Priya <priya@swiggy.com>", "Re: your application", days_ago=2, msg_id="<b>"),
    })
    found = inbox.search(client, "Swiggy", "swiggy.com")
    assert [m.message_id for m in found] == ["<a>", "<b>"]
    assert inbox.Conversation("Swiggy", found).reply_to.message_id == "<b>"


def test_search_survives_a_malformed_header():
    client = FakeIMAP({"1": b"From: \xff\xfe broken\r\nSubject: ?\r\n\r\n"})
    assert len(inbox.search(client, "Acme")) == 1


# --------------------------------------------------------------------- safety


def test_the_mailbox_is_opened_read_only():
    """It reads a personal inbox. Nothing is deleted, moved, or marked seen."""
    source = inspect.getsource(inbox.connect)
    assert "readonly=True" in source


def test_only_headers_are_fetched_when_scanning():
    """BODY.PEEK, not BODY: fetching the body would mark messages as read."""
    source = inspect.getsource(inbox.search)
    assert "BODY.PEEK" in source


def test_missing_credentials_explain_themselves():
    with pytest.raises(RuntimeError, match="same app password"):
        inbox.connect(user=None, password=None)


def test_the_draft_prompt_refuses_to_send_or_invent():
    from autowork import tailor

    assert "Do not send it" in tailor.FOLLOWUP_PROMPT
    assert "Nothing invented" in tailor.FOLLOWUP_PROMPT
    assert "Four sentences at most" in tailor.FOLLOWUP_PROMPT
