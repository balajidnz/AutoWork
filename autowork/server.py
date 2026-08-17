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
        postings = 0
        if configured:
            try:
                conn = db.connect()
                postings = conn.execute("SELECT COUNT(*) n FROM jobs").fetchone()["n"]
            except Exception:  # noqa: BLE001 — an empty db is a normal first run
                postings = 0
        return {"configured": configured, "postings": postings}

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
            if route == "/api/advise":
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
