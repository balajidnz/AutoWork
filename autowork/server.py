"""Local web server for the review console and the setup wizard.

Deliberately `http.server` from the standard library rather than Flask or
FastAPI. This binds to localhost for one person on their own machine; a web
framework would add a dependency and a version to maintain, and buy nothing.
Streamlit, which this replaces, dragged in pandas and pyarrow to render a list.

Everything is served from memory except the page itself, and the only writes
are to `profile/profiles.json` and `data/status.json`.
"""

from __future__ import annotations

import json
import secrets
import threading
from functools import partial
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

from autowork import db, present, profile_build, rank, resume_parse


def rank_load() -> dict:
    return rank.load_config()

WEB = Path(__file__).with_name("web")
# A resume is a few hundred KB; anything far larger is a mistake or an attack.
MAX_UPLOAD = 12 * 1024 * 1024

# Regenerated every run and injected into the page. Any site you visit can POST
# to localhost from your browser, and one of these endpoints opens a terminal —
# so a write is only honoured if it carries the token this process just minted.
TOKEN = secrets.token_urlsafe(24)

# One refresh at a time, and its progress, so the page can show what is
# happening rather than appearing to hang for two minutes.
REFRESH = {"running": False, "step": "", "error": "", "finished_at": ""}

# Nothing here is metered, but a refresh hits 255 career pages plus LinkedIn.
# Boards published for free do not deserve a request every thirty seconds, and
# LinkedIn will rate-limit an IP that asks too often — which would silently
# degrade a source rather than fail loudly.
REFRESH_COOLDOWN_MINUTES = 15


class Handler(BaseHTTPRequestHandler):
    server_version = "autowork"

    # ------------------------------------------------------------- plumbing

    def log_message(self, fmt: str, *args: Any) -> None:
        """Silence per-request logging; the terminal shows the URL and nothing else."""

    def _send(self, status: int, body: bytes, content_type: str) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        # This serves a person's resume and job history on localhost. No cache,
        # and no embedding it from another origin.
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Frame-Options", "DENY")
        self.end_headers()
        self.wfile.write(body)

    def _json(self, payload: dict, status: int = 200) -> None:
        self._send(status, json.dumps(payload).encode("utf-8"), "application/json")

    def _body(self) -> bytes:
        length = int(self.headers.get("Content-Length") or 0)
        if length > MAX_UPLOAD:
            raise ValueError(f"upload too large ({length:,} bytes)")
        return self.rfile.read(length)

    # ------------------------------------------------------------------ GET

    def do_GET(self) -> None:  # noqa: N802 — BaseHTTPRequestHandler's interface
        route = self.path.split("?")[0]
        try:
            if route in ("/", "/index.html"):
                page = (WEB / "index.html").read_text(encoding="utf-8")
                page = page.replace("__AUTOWORK_TOKEN__", TOKEN)
                self._send(200, page.encode("utf-8"), "text/html; charset=utf-8")
            elif route == "/api/state":
                self._json(self._state())
            elif route == "/api/families":
                self._json(profile_build.families())
            elif route == "/api/jobs":
                self._json(present.build(db.connect()))
            elif route == "/api/profile":
                # So the wizard can be reopened to edit rather than only create.
                self._json(profile_build.to_answers(rank_load()) if profile_build.exists()
                           else {"resumes": []})
            else:
                self._json({"error": "not found"}, 404)
        except Exception as exc:  # noqa: BLE001 — a stack trace must not 500 the page
            self._json({"error": f"{type(exc).__name__}: {exc}"}, 500)

    def _state(self) -> dict:
        """What the page needs before deciding whether to show setup or the list."""
        configured = profile_build.exists()
        postings, stale_days = 0, None
        if configured:
            try:
                conn = db.connect()
                postings = conn.execute("SELECT COUNT(*) n FROM jobs").fetchone()["n"]
                # How old the local corpus is. The scheduled run polls on a
                # GitHub runner and throws that database away, so a laptop that
                # never runs `poll` shows the same list for weeks — measured at
                # 13 days on a console in daily use, with nothing on screen
                # saying so.
                newest = conn.execute("SELECT MAX(last_seen) m FROM jobs").fetchone()["m"]
                if newest:
                    from datetime import UTC, datetime
                    stale_days = (datetime.now(UTC) - datetime.fromisoformat(newest)).days
            except Exception:  # noqa: BLE001 — an empty db is a normal first run
                postings = 0
        return {"configured": configured, "postings": postings,
                "staleDays": stale_days, "refresh": REFRESH}

    # ----------------------------------------------------------------- POST

    def _authorised(self) -> bool:
        """Same-origin, and carrying this run's token."""
        if self.headers.get("X-AutoWork-Token") != TOKEN:
            return False
        origin = self.headers.get("Origin")
        return origin is None or origin.startswith(("http://localhost:",
                                                    "http://127.0.0.1:"))

    def do_POST(self) -> None:  # noqa: N802
        route = self.path.split("?")[0]
        if not self._authorised():
            self._json({"error": "unauthorised — reload the page"}, 403)
            return
        try:
            if route == "/api/followup":
                self._json(self._followup(json.loads(self._body() or b"{}")))
            elif route == "/api/refresh":
                self._json(self._refresh())
            elif route == "/api/advise":
                self._json(self._advise(json.loads(self._body() or b"{}")))
            elif route == "/api/tailor":
                self._json(self._tailor(json.loads(self._body() or b"{}")))
            elif route == "/api/resume":
                self._json(self._parse_resume())
            elif route == "/api/profile":
                self._json(self._save_profile(json.loads(self._body() or b"{}")))
            elif route == "/api/status":
                self._json(self._set_status(json.loads(self._body() or b"{}")))
            else:
                self._json({"error": "not found"}, 404)
        except Exception as exc:  # noqa: BLE001
            self._json({"error": f"{type(exc).__name__}: {exc}"}, 400)

    def _parse_resume(self) -> dict:
        """Parse an uploaded resume. Nothing is written to disk here.

        The file is held in memory and only the extracted fields go back to the
        page, so a resume the user then abandons leaves no trace.
        """
        filename = self.headers.get("X-Filename", "resume.pdf")
        parsed = resume_parse.parse(self._body(), filename)
        return {
            "name": parsed.name,
            "email": parsed.email,
            "location": parsed.location,
            "years": parsed.years,
            "currentCtcLpa": parsed.current_ctc_lpa,
            "roles": parsed.roles,
            "skills": parsed.skills,
            "confident": parsed.confident,
            "families": resume_parse.families_for(parsed.roles),
            "filename": filename,
            "text": parsed.text,
        }

    def _save_profile(self, answers: dict) -> dict:
        config = profile_build.build(answers)
        if problems := profile_build.validate(config):
            return {"ok": False, "problems": problems}
        path = profile_build.save(config)
        return {"ok": True, "path": str(path.relative_to(db.REPO_ROOT))}

    def _tailor(self, payload: dict) -> dict:
        """Stage the tailoring prompt and open a terminal sitting on it."""
        from autowork import tailor as tailor_mod

        position = int(payload.get("position") or 0)
        tool = "ollama" if payload.get("tool") == "ollama" else "claude"
        prompt, ctx = tailor_mod.build_prompt(db.connect(), position)
        ctx["position"] = position
        path = tailor_mod.save_prompt(prompt, ctx, position)

        if tool == "claude" and not tailor_mod.claude_available():
            return {"ok": False, "path": str(path.relative_to(db.REPO_ROOT)),
                    "error": "Claude Code is not on your PATH. The prompt is "
                             "saved — paste it anywhere, or use Ollama."}

        script = tailor_mod.runner_script(path, ctx, tool, payload.get("model", ""))
        launched, detail = tailor_mod.launch_terminal(script)
        return {
            "ok": True, "launched": launched, "terminal": detail,
            "path": str(path.relative_to(db.REPO_ROOT)),
            "script": str(script.relative_to(db.REPO_ROOT)),
            "resume": ctx["resume"],
        }

    def _refresh(self) -> dict:
        """Poll the boards and re-rank, in the background.

        The alternative is telling someone to go and run two commands in a
        terminal, which is exactly the step that silently did not happen for
        thirteen days.
        """
        if REFRESH["running"]:
            return {"ok": True, "already": True, **REFRESH}

        if REFRESH["finished_at"]:
            from datetime import UTC, datetime
            mins = (datetime.now(UTC)
                    - datetime.fromisoformat(REFRESH["finished_at"])).total_seconds() / 60
            if mins < REFRESH_COOLDOWN_MINUTES:
                wait = int(REFRESH_COOLDOWN_MINUTES - mins) + 1
                return {"ok": False, "error":
                        f"Just refreshed. Boards update slowly — try again in "
                        f"{wait} minute{'s' if wait != 1 else ''}."}

        def work() -> None:
            from autowork import poll as poll_mod

            REFRESH.update(running=True, step="reading company career pages…", error="")
            try:
                conn = db.connect()
                poll_mod.run(conn)
                REFRESH["step"] = "scoring against your resumes…"
                rank.run(conn, rank.load_config())
                REFRESH["step"] = "clearing out closed roles…"
                db.prune(conn, rank.load_config()["constraints"]["max_age_days"])
                REFRESH["finished_at"] = db.now()
                REFRESH["step"] = ""
            except Exception as exc:  # noqa: BLE001 — surfaced on the page
                REFRESH["error"] = f"{type(exc).__name__}: {exc}"
            finally:
                REFRESH["running"] = False

        threading.Thread(target=work, daemon=True).start()
        return {"ok": True, "started": True}

    def _followup(self, payload: dict) -> dict:
        """Stage a follow-up draft in a terminal. Never sends anything.

        The recipient comes from the mailbox, not from the posting: measured on
        seven real applications, none carried an address, and guessing one is
        how a follow-up reaches a stranger.
        """
        from autowork import tailor as tailor_mod

        position = int(payload.get("position") or 0)
        tool = "ollama" if payload.get("tool") == "ollama" else "claude"
        command = (f"uv run autowork followup {position}"
                   + (f" --ollama {payload.get('model') or 'llama3.1'}"
                      if tool == "ollama" else ""))
        ctx = {"title": payload.get("title", "Follow-up"),
               "company": payload.get("company", ""),
               "resume": "drafted from the email thread",
               "missing": [], "position": position}
        path = tailor_mod.save_prompt(
            "# Staged by the console; the draft is produced by the command below.\n",
            ctx, position)
        script = tailor_mod.runner_script(path, ctx, tool, command_override=command)
        launched, detail = tailor_mod.launch_terminal(script)
        return {"ok": True, "launched": launched, "terminal": detail,
                "script": str(script.relative_to(db.REPO_ROOT))}

    def _advise(self, payload: dict) -> dict:
        """Open a model on the question of what to target.

        Same terminal handoff as tailoring: choosing target titles is a
        judgement call about a career, and a model that can read the resume and
        the current settings is better placed to argue about it than a
        frequency count.
        """
        from autowork import tailor as tailor_mod

        prompt = tailor_mod.advice_prompt(
            resumes=payload.get("resumes") or [], answers=payload
        )
        ctx = {"title": "What should I target?", "company": "AutoWork setup",
               "resume": ", ".join(r.get("label", "?") for r in payload.get("resumes") or []),
               "missing": [], "position": 0}
        path = tailor_mod.save_prompt(prompt, ctx, 0)
        tool = "ollama" if payload.get("tool") == "ollama" else "claude"
        if tool == "claude" and not tailor_mod.claude_available():
            return {"ok": False, "path": str(path.relative_to(db.REPO_ROOT)),
                    "error": "Claude Code is not on your PATH. The prompt is "
                             "saved — paste it anywhere, or use Ollama."}
        script = tailor_mod.runner_script(path, ctx, tool, payload.get("model", ""),
                                          command_override=payload.get("command"))
        launched, detail = tailor_mod.launch_terminal(script)
        return {"ok": True, "launched": launched, "terminal": detail,
                "path": str(path.relative_to(db.REPO_ROOT)),
                "script": str(script.relative_to(db.REPO_ROOT))}

    def _set_status(self, payload: dict) -> dict:
        job_id, state = payload.get("id"), payload.get("state")
        if not job_id or not state:
            return {"ok": False, "error": "id and state are required"}
        db.set_state(db.connect(), job_id, state)
        return {"ok": True, "id": job_id, "state": state}


def serve(port: int = 8765, open_browser: bool = True) -> None:
    url = f"http://localhost:{port}"
    # 127.0.0.1, not 0.0.0.0: this exposes a resume and a job history with no
    # authentication, and has no business being reachable from the network.
    httpd = ThreadingHTTPServer(("127.0.0.1", port), partial(Handler))
    print(f"AutoWork: {url}   (ctrl-c to stop)")
    if open_browser:
        import webbrowser

        threading.Timer(0.7, webbrowser.open, args=(url,)).start()
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\nstopped")
    finally:
        httpd.server_close()
