"""Command line entry point: autowork <verify|poll|stats|export>."""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path

from . import comp as comp_mod
from . import contact as contact_mod
from . import coverage as cov, db, deliver, digest, poll, rank, resume, track, watchlist


def cmd_verify(args: argparse.Namespace) -> int:
    companies = watchlist.load_companies()
    if args.company:
        companies = args.company
    if not companies:
        print("no companies to verify — populate data/companies.txt", file=sys.stderr)
        return 1
    if args.limit:
        companies = companies[: args.limit]

    ats_list = [args.ats] if args.ats else list(watchlist.ADAPTERS)
    probe_count = sum(len(watchlist.token_variants(c)) for c in companies)
    print(
        f"probing {len(companies)} companies "
        f"(~{probe_count} tokens x up to {len(ats_list)} ATS)…"
    )

    hits = asyncio.run(
        watchlist.verify(companies, ats_list=ats_list, concurrency=args.concurrency)
    )

    conn = db.connect()
    watchlist.persist(conn, hits)

    hits.sort(key=lambda p: -p.job_count)
    for probe in hits:
        print(f"  ✓ {probe.ats:<11} {probe.token:<24} {probe.job_count:>5} jobs  ({probe.company})")

    total = db.verified_boards(conn)
    written = db.export_boards(conn)
    print(
        f"\nresolved {len(hits)}/{len(companies)} companies this run; "
        f"{len(total)} verified boards on the watchlist "
        f"({written} saved to {db.BOARDS_JSON.relative_to(db.REPO_ROOT)})"
    )
    return 0


def cmd_poll(args: argparse.Namespace) -> int:
    conn = db.connect()
    stats = poll.run(conn, concurrency=args.concurrency)
    if not stats["boards"]:
        print("no verified boards — run `autowork verify` first", file=sys.stderr)
        return 1

    print(
        f"polled {stats['boards']} boards"
        + (f" + {stats['searched']} from search" if stats.get("searched") else "")
        + f" -> {stats['fetched']} postings "
        f"({stats['new']} new, {stats['updated']} already known)"
    )
    if stats["errors"]:
        print(f"{len(stats['errors'])} board(s) failed this run:")
        for line in stats["errors"][:10]:
            print(f"  ! {line}")

    if args.dump:
        written = db.export_jsonl(conn)
        print(f"dumped {written} rows to {db.JOBS_JSONL.relative_to(db.REPO_ROOT)} (not committed)")
    return 0


def cmd_stats(args: argparse.Namespace) -> int:
    conn = db.connect()
    total = conn.execute("SELECT COUNT(*) AS n FROM jobs").fetchone()["n"]
    if not total:
        print("no jobs yet — run `autowork verify` then `autowork poll`")
        return 0

    print(f"{total} postings from {len(db.verified_boards(conn))} boards\n")

    print("by source:")
    for row in conn.execute(
        "SELECT source, COUNT(*) AS n FROM jobs GROUP BY source ORDER BY n DESC"
    ):
        print(f"  {row['source']:<12} {row['n']:>6}")

    early = db.ats_only_keys(conn)
    print(f"\nATS-only (not yet on any aggregator): {len(early)}")

    print("\nmost recent postings:")
    for row in conn.execute(
        """SELECT company, title, location, posted_at FROM jobs
           WHERE posted_at IS NOT NULL ORDER BY posted_at DESC LIMIT ?""",
        (args.limit,),
    ):
        loc = (row["location"] or "—")[:28]
        print(f"  {row['posted_at'][:10]}  {row['company'][:18]:<18}  {loc:<28}  {row['title'][:52]}")
    return 0


def cmd_rank(args: argparse.Namespace) -> int:
    conn = db.connect()
    cfg = rank.load_config()
    stats = rank.run(conn, cfg)
    if not stats["jobs"]:
        print("no jobs to rank — run `autowork poll` first", file=sys.stderr)
        return 1

    print(
        f"ranked {stats['jobs']} postings -> {stats['eligible']} core"
        f" + {stats['stretch']} stretch\n"
    )
    print("rejected by gate:")
    for reason, count in sorted(stats["gated"].items(), key=lambda kv: -kv[1]):
        print(f"  {reason:<12} {count:>6}")
    return 0


def cmd_top(args: argparse.Namespace) -> int:
    conn = db.connect()
    rows = rank.shortlist(conn, limit=args.limit, tier=None if args.all else "core")
    if not rows:
        print("nothing eligible — run `autowork rank`", file=sys.stderr)
        return 1

    for i, row in enumerate(rows, 1):
        reasons = ", ".join(json.loads(row["reasons"])[: args.reasons])
        loc = (row["location"] or "—")[:26]
        tag = "*" if row["tier"] == "stretch" else " "
        print(f"{i:>3}.{tag}[{row['score']:>5.1f}] {row['profile']:<7} {row['company'][:16]:<16} {row['title'][:48]}")
        print(f"      {loc:<26}  {reasons}")
        if args.coverage:
            owned = cov.candidate_terms(rank.load_config())
            print(f"      gaps: {cov.analyse(row['description'], owned).summary()}")
        if args.urls:
            print(f"      {row['url']}")
    return 0


def cmd_both(args: argparse.Namespace) -> int:
    """Postings strong on both resumes — where the combined profile is rare."""
    conn = db.connect()
    rows = rank.both_track_jobs(conn, threshold=args.threshold)
    print(f"{len(rows)} postings score >= {args.threshold} on BOTH resumes\n")
    for i, row in enumerate(rows[: args.limit], 1):
        print(
            f"{i:>3}. [{row['weakest']:>5.1f}/{row['best']:>5.1f}] "
            f"{row['company'][:16]:<16} {(row['location'] or '—')[:24]:<24} {row['title'][:46]}"
        )
    return 0


def cmd_digest(args: argparse.Namespace) -> int:
    conn = db.connect()
    d = digest.build(conn, standing_limit=args.standing)
    if not d.new and not d.standing:
        print("nothing eligible — run `autowork rank` first", file=sys.stderr)
        return 1

    path = digest.write_xlsx(d)
    print(f"{d.day}: {len(d.new)} new, {len(d.standing)} standing")
    print(f"workbook -> {path.relative_to(db.REPO_ROOT)}")

    for row in d.new[: args.preview]:
        print(f"  [{row['score']:>5.1f}] {row['company'][:16]:<16} {row['title'][:52]}")

    if args.dry_run:
        preview = db.DIGEST_DIR / f"autowork-{d.day}.html"
        preview.write_text(digest.render_html(d), encoding="utf-8")
        print(f"dry run — email not sent; preview -> {preview.relative_to(db.REPO_ROOT)}")
        return 0

    channels = args.channel.split(",") if args.channel else deliver.default_channels()
    if not channels:
        print(
            "no delivery channel configured — set SMTP_USER/SMTP_PASS or "
            "TELEGRAM_BOT_TOKEN/TELEGRAM_CHAT_ID, or use --dry-run",
            file=sys.stderr,
        )
        return 1

    results = deliver.deliver(d, channels)
    for r in results:
        print(f"  {'✓' if r.ok else '✗'} {r.channel}: {r.detail}")

    # Mark seen only if at least one channel delivered. Doing it unconditionally
    # would burn the day's postings on a failed send — they would be recorded as
    # shown and never appear in another digest.
    if not any(r.ok for r in results):
        print("nothing delivered — ledger left untouched", file=sys.stderr)
        return 1

    if args.no_ledger:
        # Real send, no bookkeeping. Without this, testing a new channel costs
        # you the day's postings: they get marked as shown and drop out of
        # tomorrow's "new" section.
        print("--no-ledger: delivered, but not recorded as shown")
        return 0

    total = db.mark_seen(r["id"] for r in d.new)
    db.export_digest([dict(r) for r in d.new], d.day)
    print(f"ledger now {total} ids")
    return 0


def cmd_contacts(args: argparse.Namespace) -> int:
    """Resolve who to write to for everything on the shortlist."""
    conn = db.connect()
    rows = rank.shortlist(conn, 10_000, tier=None)
    cache = contact_mod.enrich(rows)
    contact_mod.save(cache)

    seen, with_email, mail_ok = set(), 0, 0
    for row in rows:
        key = db.company_slug(row["company"])
        if key in seen:
            continue
        seen.add(key)
        c = cache.get(key)
        if not c:
            continue
        if c.found_emails:
            with_email += 1
            print(f"  ✉  {row['company'][:22]:<22} {c.summary()}")
        elif c.generic_emails:
            print(f"  ▫  {row['company'][:22]:<22} {c.summary()}")
        elif c.mx:
            mail_ok += 1

    print(f"\n{with_email}/{len(seen)} companies name a person in the posting")
    print(f"{mail_ok} more have a resolved mail domain (name still needed)")
    if args.name:
        first, _, last = args.name.partition(" ")
        key = db.company_slug(args.company or "")
        c = cache.get(key)
        if not c:
            print(f"\nno contact record for {args.company!r}", file=sys.stderr)
            return 1
        print(f"\nunverified address forms for {args.name} at {c.domain}:")
        for guess in c.guesses(first, last):
            print(f"  {guess}")
    return 0


def cmd_telegram_setup(args: argparse.Namespace) -> int:
    """Discover the chat id for a bot token."""
    import os

    token = args.token or os.environ.get("TELEGRAM_BOT_TOKEN")
    if not token:
        print(
            "usage: autowork telegram-setup --token <BotFather token>\n\n"
            "  1. message @BotFather on Telegram, /newbot, copy the token\n"
            "  2. send your new bot any message (it cannot find you otherwise)\n"
            "  3. rerun this with --token",
            file=sys.stderr,
        )
        return 1
    try:
        info = deliver.inspect_bot(token)
    except Exception as exc:  # noqa: BLE001
        print(f"telegram: {exc}", file=sys.stderr)
        return 1

    handle = f"@{info.username}" if info.username else "your bot"
    print(f"bot: {handle}")

    if info.webhook:
        print(
            f"\n  A webhook is registered ({info.webhook}).\n"
            "  Telegram delivers updates to it *instead of* getUpdates, so no chats\n"
            "  will ever appear here. Clear it and rerun:\n"
            f"    curl -s 'https://api.telegram.org/bot<token>/deleteWebhook'",
            file=sys.stderr,
        )
        return 1

    if not info.chats:
        print(
            f"\n  No chats yet. Open Telegram, search for {handle}, and send it\n"
            "  any message — /start is fine. Telegram will not reveal a chat id\n"
            "  until the bot has heard from you. Then rerun this.",
            file=sys.stderr,
        )
        return 1

    for chat_id, who in info.chats:
        print(f"  TELEGRAM_CHAT_ID={chat_id}   ({who})")
    print("\nadd that plus TELEGRAM_BOT_TOKEN as repository secrets")
    return 0


def cmd_render(args: argparse.Namespace) -> int:
    """Markdown resume -> PDF you can actually upload."""
    source = Path(args.source)
    if not source.exists():
        print(f"no such file: {source}", file=sys.stderr)
        return 1
    out = Path(args.out) if args.out else None
    written = resume.render_file(source, out, body_pt=args.size)
    print(f"{source.name} -> {written}  ({written.stat().st_size:,} bytes)")
    return 0


def cmd_track(args: argparse.Namespace) -> int:
    """Pipeline state and anything due a follow-up."""
    conn = db.connect()
    apps = track.load(conn)
    if not apps:
        print("nothing tracked yet — mark roles applied in the console")
        return 0

    if args.set:
        job_id, state = args.set
        if state not in track.PIPELINE and state != "skipped":
            print(f"state must be one of: {', '.join(track.PIPELINE)}, skipped", file=sys.stderr)
            return 1
        db.set_state(conn, job_id, state, note=args.note)
        print(f"{job_id} -> {state}")
        return 0

    counts = track.summary(apps)
    rate = track.response_rate(apps)
    print("pipeline:")
    for state in track.PIPELINE:
        if counts[state]:
            print(f"  {state:<12} {counts[state]}")
    print(f"\n  {counts['live']} live · {track.applied_this_week(apps)} applied this week", end="")
    print(f" · {rate:.0%} response rate" if rate is not None else "")

    due = track.follow_ups(apps)
    if due:
        print(f"\ndue a follow-up ({len(due)}):")
        for a in due:
            print(f"  {a.days_since:>3}d  {a.company[:20]:<20} {a.title[:40]}")
            if a.url:
                print(f"        {a.url}")
    cold = [a for a in apps if a.went_cold]
    if cold:
        print(f"\ngone cold — no reply after {track.GIVE_UP_AFTER_DAYS}d ({len(cold)}):")
        for a in cold:
            print(f"  {a.days_since:>3}d  {a.company[:20]:<20} {a.title[:40]}")
    return 0


def cmd_comp(args: argparse.Namespace) -> int:
    """Populate compensation estimates for everything on the shortlist."""
    conn = db.connect()
    rows = rank.shortlist(conn, 10_000, tier=None)
    pairs = [(r["company"], r["title"]) for r in rows]
    before = len(comp_mod.load())
    cache = comp_mod.enrich(pairs)
    comp_mod.save(cache)
    print(f"{len(rows)} roles · cache {before} -> {len(cache)} entries\n")

    floor = rank.load_config()["candidate"].get("current_ctc_lpa")
    hits = [(r, comp_mod.lookup(cache, r["company"], r["title"])) for r in rows]
    hits = [(r, c) for r, c in hits if c and c.found]
    hits.sort(key=lambda rc: -(rc[1].median or 0))
    for r, c in hits:
        flag = "  " if not floor or (c.median or 0) >= floor else " !"
        print(f"{flag}{r['company'][:18]:<18} {r['title'][:34]:<34} {c.summary(floor)}")
    print(f"\n{len(hits)}/{len(rows)} have comp data")
    return 0


def cmd_show(args: argparse.Namespace) -> int:
    """Emit one shortlist entry in full — the input to /fit and /tailor."""
    conn = db.connect()
    rows = rank.shortlist(conn, 10_000, tier=None)
    if not 1 <= args.n <= len(rows):
        print(f"pick 1..{len(rows)} (see `autowork top --all`)", file=sys.stderr)
        return 1
    row = rows[args.n - 1]
    owned = cov.candidate_terms(rank.load_config())
    gaps = cov.analyse(row["description"], owned)

    payload = {
        "rank": args.n,
        "id": row["id"],
        "title": row["title"],
        "company": row["company"],
        "location": row["location"],
        "url": row["url"],
        "posted_at": row["posted_at"],
        "score": row["score"],
        "tier": row["tier"],
        "best_profile": row["profile"],
        "match_reasons": json.loads(row["reasons"] or "[]"),
        "requirements_covered": gaps.have,
        "requirements_missing": gaps.missing,
        "coverage": round(gaps.ratio, 2),
        "description": row["description"],
    }
    print(json.dumps(payload, indent=2, ensure_ascii=False))
    return 0


def cmd_reset(args: argparse.Namespace) -> int:
    """Clear the previous owner's data so a fresh clone belongs to you.

    The repository ships one person's profile, resumes and application history
    alongside the parts worth sharing. Without this, cloning it means inheriting
    someone else's job search: the setup wizard never appears (a profile already
    exists), and the shortlist is scored against their resume.

    The 254 verified boards, the company list and the salary cache are kept —
    they took hours of probing to build and are the same for everybody.
    """
    personal = [
        db.REPO_ROOT / "profile" / "profiles.json",
        db.STATUS_JSON,
        db.SEEN_TXT,
        db.DB_PATH,
        db.JOBS_JSONL,
        *(db.REPO_ROOT / "profile").glob("resume-*.md"),
        *(db.REPO_ROOT / "profile" / "tailored").glob("*.md"),
        *db.DIGEST_DIR.glob("*"),
        *(db.DATA_DIR / "tailor").glob("*"),
    ]
    existing = [p for p in personal if p.exists()]
    if not existing:
        print("nothing to clear — this is already a fresh setup")
        return 0

    print("This will delete:")
    for path in existing:
        print(f"  {path.relative_to(db.REPO_ROOT)}")
    print("\nKept: data/boards.json, data/companies.txt, data/comp.json "
          "(shared, and expensive to rebuild)")

    if not args.yes:
        try:
            if input("\nDelete these? [y/N] ").strip().lower() not in ("y", "yes"):
                print("cancelled")
                return 1
        except EOFError:
            print("cancelled (no terminal to confirm on — rerun with --yes)")
            return 1

    for path in existing:
        path.unlink()
    print(f"\ncleared {len(existing)} file(s). Next: uv run autowork console")
    return 0


def cmd_console(args: argparse.Namespace) -> int:
    """Launch the review console — the setup wizard on a first run."""
    from autowork import profile_build, server

    if not profile_build.exists():
        print("no profile yet — the browser will open the setup wizard")
    server.serve(port=args.port, open_browser=not args.no_browser)
    return 0


def cmd_tailor(args: argparse.Namespace) -> int:
    """Rewrite a resume for one posting, using whatever model is available."""
    from autowork import tailor as tailor_mod

    conn = db.connect()
    try:
        prompt, ctx = tailor_mod.build_prompt(
            conn, args.n, Path(args.resume) if args.resume else None
        )
    except (ValueError, FileNotFoundError) as exc:
        print(exc, file=sys.stderr)
        return 1

    print(f"# {ctx['title']} — {ctx['company']}", file=sys.stderr)
    print(f"# resume: {ctx['resume']}", file=sys.stderr)
    if ctx["missing"]:
        print(f"# not evidenced: {', '.join(ctx['missing'])}", file=sys.stderr)

    if not args.ollama:
        # Straight to stdout so it can be piped or pasted anywhere. The notes
        # above go to stderr, so `autowork tailor 3 > prompt.txt` stays clean.
        print(prompt)
        print(
            "\n# ^ prompt only. Run it with a local model via --ollama <model>, "
            "or use /tailor in Claude Code.",
            file=sys.stderr,
        )
        return 0

    print(f"# generating with ollama/{args.ollama}…", file=sys.stderr)
    try:
        print(tailor_mod.run_ollama(prompt, args.ollama))
    except Exception as exc:  # noqa: BLE001
        print(f"ollama failed: {exc}", file=sys.stderr)
        return 1
    return 0


def cmd_doctor(args: argparse.Namespace) -> int:
    """Report configuration so a CI failure is diagnosable from the log alone."""
    print("delivery configuration:")
    for line in deliver.diagnose():
        print(line)

    conn = db.connect()
    boards = db.verified_boards(conn) or (db.import_boards(conn), db.verified_boards(conn))[1]
    jobs = conn.execute("SELECT COUNT(*) n FROM jobs").fetchone()["n"]
    # DISTINCT: there is one score row per (job, profile), so a plain COUNT(*)
    # reports double the number of actual postings.
    scored = conn.execute(
        "SELECT COUNT(DISTINCT job_id) n FROM scores WHERE passed = 1"
    ).fetchone()["n"]
    print("\nstate:")
    print(f"  boards on watchlist  {len(boards)}")
    print(f"  postings in database {jobs}")
    print(f"  eligible after rank  {scored}")
    print(f"  ledger               {len(db.load_seen())} ids")
    return 0


def cmd_export(args: argparse.Namespace) -> int:
    conn = db.connect()
    print(f"wrote {db.export_jsonl(conn)} rows to {db.JOBS_JSONL} (not committed)")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="autowork", description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("verify", help="resolve company names to ATS board tokens")
    p.add_argument("company", nargs="*", help="override data/companies.txt")
    p.add_argument("--ats", choices=list(watchlist.ADAPTERS), help="probe a single ATS")
    p.add_argument("--limit", type=int, help="only the first N companies")
    p.add_argument("--concurrency", type=int, default=12)
    p.set_defaults(func=cmd_verify)

    p = sub.add_parser("poll", help="fetch postings from every verified board")
    p.add_argument("--concurrency", type=int, default=12)
    p.add_argument("--dump", action="store_true", help="also write the full corpus locally")
    p.set_defaults(func=cmd_poll)

    p = sub.add_parser("stats", help="summarise what is in the database")
    p.add_argument("--limit", type=int, default=15)
    p.set_defaults(func=cmd_stats)

    p = sub.add_parser("rank", help="score every posting against both profiles")
    p.set_defaults(func=cmd_rank)

    p = sub.add_parser("top", help="show the current shortlist")
    p.add_argument("--limit", type=int, default=25)
    p.add_argument("--reasons", type=int, default=4, help="reasons shown per row")
    p.add_argument("--urls", action="store_true")
    p.add_argument("--all", action="store_true", help="include stretch-tier roles")
    p.add_argument("--coverage", action="store_true", help="show requirement gaps vs the resume")
    p.set_defaults(func=cmd_top)

    p = sub.add_parser("both", help="postings strong on both resumes")
    p.add_argument("--threshold", type=float, default=30.0)
    p.add_argument("--limit", type=int, default=25)
    p.set_defaults(func=cmd_both)

    p = sub.add_parser("digest", help="build the daily workbook and email it")
    p.add_argument("--dry-run", action="store_true", help="write an HTML preview, do not send")
    p.add_argument("--standing", type=int, default=30, help="size of the standing shortlist")
    p.add_argument("--preview", type=int, default=8, help="rows echoed to the terminal")
    p.add_argument(
        "--channel",
        help="comma-separated: email,telegram (default: whatever is configured)",
    )
    p.add_argument(
        "--no-ledger",
        action="store_true",
        help="send for real but do not mark the postings as shown",
    )
    p.set_defaults(func=cmd_digest)

    p = sub.add_parser("show", help="print one shortlist entry as JSON")
    p.add_argument("n", type=int, help="position in `autowork top --all`")
    p.set_defaults(func=cmd_show)

    p = sub.add_parser("contacts", help="who to write to, per company")
    p.add_argument("--company", help="company to build address guesses for")
    p.add_argument("--name", help='person, e.g. "Priya Sharma"')
    p.set_defaults(func=cmd_contacts)

    p = sub.add_parser("telegram-setup", help="find your Telegram chat id")
    p.add_argument("--token", help="bot token from BotFather")
    p.set_defaults(func=cmd_telegram_setup)

    p = sub.add_parser("render", help="render a markdown resume to PDF")
    p.add_argument("source", help="path to a .md resume")
    p.add_argument("-o", "--out", help="output path (default: alongside the source)")
    p.add_argument("--size", type=float, default=9.0, help="body point size")
    p.set_defaults(func=cmd_render)

    p = sub.add_parser("track", help="application pipeline and follow-ups")
    p.add_argument("--set", nargs=2, metavar=("JOB_ID", "STATE"), help="update one application")
    p.add_argument("--note", help="note to attach with --set")
    p.set_defaults(func=cmd_track)

    p = sub.add_parser("comp", help="fetch salary estimates for the shortlist")
    p.set_defaults(func=cmd_comp)

    p = sub.add_parser("console", help="launch the Streamlit review console")
    p.add_argument("--port", type=int, default=8765)
    p.add_argument("--no-browser", action="store_true",
                   help="do not open a browser window")
    p.set_defaults(func=cmd_console)

    p = sub.add_parser("tailor", help="tailor a resume for one posting")
    p.add_argument("n", type=int, help="rank number from the console or `top --all`")
    p.add_argument("--ollama", metavar="MODEL",
                   help="generate with a local Ollama model, e.g. llama3.1")
    p.add_argument("--resume", help="override which resume file to adapt")
    p.set_defaults(func=cmd_tailor)

    p = sub.add_parser("reset", help="clear the previous owner's profile and history")
    p.add_argument("--yes", action="store_true", help="skip the confirmation")
    p.set_defaults(func=cmd_reset)

    p = sub.add_parser("doctor", help="report delivery config and pipeline state")
    p.set_defaults(func=cmd_doctor)

    p = sub.add_parser("export", help="rewrite data/jobs.jsonl from the database")
    p.set_defaults(func=cmd_export)

    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
