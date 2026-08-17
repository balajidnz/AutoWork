"""Delivery channels for the digest.

Kept separate from `digest.py` so building and sending are independent: the
workbook and HTML render identically no matter where they end up, and adding a
channel never touches the assembly code.

Channels are selected by name and run independently — one failing does not stop
the others, because a Telegram outage should not also cost you the email.
"""

from __future__ import annotations

import os
import smtplib
import sqlite3
from dataclasses import dataclass, field
from email.message import EmailMessage

import httpx

from .digest import Digest, _age_days, _reasons, render_html

TELEGRAM_API = "https://api.telegram.org"
# Telegram hard-rejects a sendMessage body over 4096 characters. Leave room for
# the header and footer rather than discovering the limit in production.
TELEGRAM_LIMIT = 3800


@dataclass(slots=True)
class Result:
    channel: str
    ok: bool
    detail: str


# --------------------------------------------------------------------- email


def smtp_password() -> str | None:
    """Accept either spelling.

    `SMTP_PASSWORD` is the more natural name and gets reached for first; making
    only `SMTP_PASS` work turns a reasonable guess into a silent misconfiguration.
    """
    return os.environ.get("SMTP_PASS") or os.environ.get("SMTP_PASSWORD")


def send_email(digest: Digest, *, host: str = "smtp.gmail.com", port: int = 587) -> Result:
    user = os.environ.get("SMTP_USER")
    password = smtp_password()
    to = os.environ.get("DIGEST_TO") or user
    if not (user and password and to):
        missing = [
            n for n, v in
            [("SMTP_USER", user), ("SMTP_PASS/SMTP_PASSWORD", password), ("DIGEST_TO", to)]
            if not v
        ]
        return Result("email", False, f"not set: {', '.join(missing)}")

    msg = EmailMessage()
    msg["Subject"] = f"AutoWork · {len(digest.new)} new · {digest.day}"
    msg["From"], msg["To"] = user, to
    msg.set_content(
        f"{len(digest.new)} new roles, {len(digest.standing)} on the shortlist.\n"
        "HTML view recommended; full list attached."
    )
    msg.add_alternative(render_html(digest), subtype="html")

    if digest.xlsx and digest.xlsx.exists():
        msg.add_attachment(
            digest.xlsx.read_bytes(),
            maintype="application",
            subtype="vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            filename=digest.xlsx.name,
        )

    try:
        with smtplib.SMTP(host, port, timeout=30) as smtp:
            smtp.starttls()
            smtp.login(user, password)
            smtp.send_message(msg)
    except smtplib.SMTPAuthenticationError as exc:
        return Result("email", False, f"{exc.smtp_code} {_auth_hint(password)}")
    except Exception as exc:  # noqa: BLE001 — report, never abort the run
        return Result("email", False, f"{type(exc).__name__}: {exc}")
    return Result("email", True, to)


def _auth_hint(password: str) -> str:
    """Turn Gmail's opaque 535 into something actionable.

    Every one of these presents identically as "Username and Password not
    accepted", so the shape of the secret is the only clue available.
    """
    if " " in password:
        return "rejected — SMTP_PASS contains spaces; paste the app password unspaced"
    if len(password) != 16:
        return (
            f"rejected — SMTP_PASS is {len(password)} chars; a Google app "
            "password is exactly 16 (an account password will not work)"
        )
    return "rejected — app password looks well-formed; check SMTP_USER matches the account that generated it"


# ------------------------------------------------------------------ telegram


def _tg_escape(text: str) -> str:
    """Telegram's HTML parser rejects a message containing a stray '<' or '&'."""
    return (
        str(text or "").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    )


def render_telegram(digest: Digest, max_rows: int = 10) -> str:
    """Compact message body.

    Telegram accepts only a small HTML subset — b, i, a, code — so this cannot
    reuse the email template's tables and inline styles.
    """
    lines = [
        f"<b>AutoWork · {digest.day}</b>",
        f"{len(digest.new)} new · {len(digest.standing)} shortlisted"
        f" · {len(digest.stretch)} stretch",
        "",
    ]
    rows = digest.new or digest.standing
    header = "New today" if digest.new else "Nothing new — standing shortlist"
    lines.append(f"<b>{header}</b>")

    shown = 0
    for row in rows[:max_rows]:
        age = _age_days(row["posted_at"])
        meta = " · ".join(
            p for p in [row["company"], row["location"] or "", f"{age}d" if age is not None else ""] if p
        )
        block = (
            f"\n<b>{row['score']:.0f}</b> "
            f"<a href=\"{_tg_escape(row['url'])}\">{_tg_escape(row['title'])}</a>\n"
            f"{_tg_escape(meta)}\n"
            f"<i>{_tg_escape(_reasons(row, 3))}</i>"
        )
        if sum(len(x) for x in lines) + len(block) > TELEGRAM_LIMIT:
            break
        lines.append(block)
        shown += 1

    remaining = len(rows) - shown
    if remaining > 0:
        lines.append(f"\n<i>+{remaining} more in the attached workbook.</i>")
    return "\n".join(lines)


def send_telegram(digest: Digest) -> Result:
    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    chat_id = os.environ.get("TELEGRAM_CHAT_ID")
    if not (token and chat_id):
        return Result("telegram", False, "TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID not set")

    base = f"{TELEGRAM_API}/bot{token}"
    try:
        with httpx.Client(timeout=30.0) as client:
            resp = client.post(
                f"{base}/sendMessage",
                json={
                    "chat_id": chat_id,
                    "text": render_telegram(digest),
                    "parse_mode": "HTML",
                    # Otherwise Telegram renders a preview card for the first
                    # job link and buries the rest of the list below the fold.
                    "disable_web_page_preview": True,
                },
            )
            payload = resp.json()
            if not payload.get("ok"):
                return Result("telegram", False, payload.get("description", resp.text[:200]))

            if digest.xlsx and digest.xlsx.exists():
                doc = client.post(
                    f"{base}/sendDocument",
                    data={"chat_id": chat_id},
                    files={
                        "document": (
                            digest.xlsx.name,
                            digest.xlsx.read_bytes(),
                            "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                        )
                    },
                )
                if not doc.json().get("ok"):
                    # The list already landed; the workbook is a bonus.
                    return Result("telegram", True, f"chat {chat_id} (message only)")
    except Exception as exc:  # noqa: BLE001
        return Result("telegram", False, f"{type(exc).__name__}: {exc}")
    return Result("telegram", True, f"chat {chat_id}")


@dataclass(slots=True)
class BotInfo:
    username: str | None = None
    webhook: str | None = None
    chats: list[tuple[str, str]] = field(default_factory=list)


def inspect_bot(token: str) -> BotInfo:
    """Everything needed to finish Telegram setup, in one round trip each.

    `getUpdates` alone cannot explain its own empty result, which made the
    original "no chats yet" message a dead end — it never said *which* bot to
    message. So this also reports the bot's @username, and whether a webhook is
    registered, since a webhook silently suppresses `getUpdates` entirely.
    """
    info = BotInfo()
    with httpx.Client(timeout=30.0) as client:
        me = client.get(f"{TELEGRAM_API}/bot{token}/getMe").json()
        if not me.get("ok"):
            raise RuntimeError(me.get("description", "getMe failed — is the token right?"))
        info.username = (me.get("result") or {}).get("username")

        hook = client.get(f"{TELEGRAM_API}/bot{token}/getWebhookInfo").json()
        info.webhook = ((hook.get("result") or {}).get("url") or None) if hook.get("ok") else None

        payload = client.get(f"{TELEGRAM_API}/bot{token}/getUpdates").json()
    if not payload.get("ok"):
        raise RuntimeError(payload.get("description", "getUpdates failed"))

    found: dict[str, str] = {}
    for update in payload.get("result", []):
        chat = (update.get("message") or update.get("channel_post") or {}).get("chat")
        if chat:
            name = chat.get("username") or chat.get("title") or chat.get("first_name") or "?"
            found[str(chat["id"])] = f"{name} ({chat.get('type')})"
    info.chats = sorted(found.items())
    return info


def resolve_chat_id(token: str) -> list[tuple[str, str]]:
    return inspect_bot(token).chats


# ------------------------------------------------------------------ dispatch

CHANNELS = {"email": send_email, "telegram": send_telegram}


def deliver(digest: Digest, channels: list[str]) -> list[Result]:
    return [CHANNELS[name](digest) for name in channels if name in CHANNELS]


def diagnose() -> list[str]:
    """Report delivery configuration without printing secrets into CI logs."""
    def show(name: str, *, secret: bool = False) -> str:
        value = os.environ.get(name)
        if not value:
            return f"  ✗ {name:<20} not set"
        shown = f"{len(value)} chars" if secret else value
        return f"  ✓ {name:<20} {shown}"

    lines = [
        show("SMTP_USER"),
        show("SMTP_PASS", secret=True),
        show("SMTP_PASSWORD", secret=True),
        show("DIGEST_TO"),
        show("TELEGRAM_BOT_TOKEN", secret=True),
        show("TELEGRAM_CHAT_ID"),
        show("DIGEST_CHANNEL"),
    ]
    password = smtp_password() or ""
    if password and (" " in password or len(password) != 16):
        lines.append(f"  ! SMTP_PASS {_auth_hint(password)}")
    lines.append(f"  → channels: {default_channels() or 'none'}")
    return lines


def default_channels() -> list[str]:
    """Whatever is configured, so an unset channel is skipped rather than failing."""
    configured = os.environ.get("DIGEST_CHANNEL")
    if configured:
        return [c.strip() for c in configured.split(",") if c.strip() in CHANNELS]
    available = []
    if os.environ.get("TELEGRAM_BOT_TOKEN") and os.environ.get("TELEGRAM_CHAT_ID"):
        available.append("telegram")
    if os.environ.get("SMTP_USER") and smtp_password():
        available.append("email")
    return available
