"""Tailor a resume for one posting, with whatever model the user has.

There are three ways people run this and none of them should be privileged:

* **Claude Code** — `/tailor <n>`. The best output, and free on a Claude
  subscription, but only if you already have one.
* **Ollama** — `autowork tailor <n> --ollama llama3.1`. Local, free, private,
  and nothing leaves the machine.
* **Neither** — `autowork tailor <n>` prints the prompt so it can be pasted
  into any chat window.

The prompt is built once, here, so all three get the same instructions. The
truthfulness constraint in particular is not optional: a resume that wins a
screen on an invented skill loses the interview, which is worse than not
getting the screen.
"""

from __future__ import annotations

import json
from pathlib import Path

from autowork import coverage as cov
from autowork import db, rank

OLLAMA = "http://localhost:11434"

PROMPT = """\
You are rewriting a resume for one specific job posting.

## The posting

{job}

## The resume to adapt

{resume}

## Rules

Every claim in your output must trace to a line in the resume above. You may
re-order, re-weight, re-word, promote a buried detail, drop anything
irrelevant, and change emphasis freely. You may NOT invent a technology, a
metric, a scope, or a responsibility that is not already there.

If the posting wants something this person has genuinely not done, leave it
out. Do not imply it. A resume that wins a screen and loses the interview is
worse than one that never got the screen.

Keep every number — they are the strongest thing on the page.

The posting asks for these, and the resume does not currently evidence them:
{missing}
Only add one of those if the resume supports it under a different name. Say
which, and why, in your notes.

## Output

1. The tailored resume, in markdown, one page of content.
2. Then, separately, under a "Notes" heading: the three biggest changes you
   made and why, and every item above you could not honestly address. That
   last list is what this person prepares for in the interview, so do not
   soften it.
"""


def job_brief(row) -> str:
    """The posting, trimmed to what actually informs a rewrite."""
    return "\n".join([
        f"Title: {row['title']}",
        f"Company: {row['company']}",
        f"Location: {row['location'] or 'not stated'}",
        f"URL: {row['url']}",
        "",
        (row["description"] or "")[:6000],
    ])


def build_prompt(conn, position: int, resume_path: Path | None = None) -> tuple[str, dict]:
    """(prompt, context) for the posting at `position` in the shortlist.

    `position` is the number shown in the console and by `autowork top --all`;
    all three index the same unfiltered, score-ordered list.
    """
    rows = rank.shortlist(conn, 10_000, tier=None)
    if not 1 <= position <= len(rows):
        raise ValueError(f"pick 1..{len(rows)} — see `autowork top --all`")
    row = rows[position - 1]

    config = rank.load_config()
    profile = (config["profiles"].get(row["profile"]) or {})
    path = resume_path or _resume_path(profile)
    if not path or not path.exists():
        raise FileNotFoundError(
            f"no resume file for the '{row['profile']}' profile. Set "
            f"profiles.{row['profile']}.resume in profile/profiles.json, or "
            f"pass --resume <file>."
        )

    gaps = cov.analyse(row["description"], cov.candidate_terms(config))
    missing = ", ".join(gaps.missing) or "nothing — the resume covers every term detected"
    prompt = PROMPT.format(
        job=job_brief(row),
        resume=path.read_text(encoding="utf-8"),
        missing=missing,
    )
    return prompt, {
        "title": row["title"], "company": row["company"], "profile": row["profile"],
        "resume": str(path), "missing": gaps.missing, "url": row["url"],
    }


def _resume_path(profile: dict) -> Path | None:
    """Resolve the configured resume, accepting a repo-relative or absolute path."""
    raw = (profile.get("resume") or "").strip()
    if not raw:
        return None
    path = Path(raw)
    return path if path.is_absolute() else db.REPO_ROOT / path


def ollama_models() -> list[str]:
    """Locally installed models, or [] if Ollama is not running."""
    import httpx

    try:
        resp = httpx.get(f"{OLLAMA}/api/tags", timeout=3.0)
        return [m["name"] for m in resp.json().get("models", [])]
    except Exception:  # noqa: BLE001 — not running is the normal case
        return []


def run_ollama(prompt: str, model: str, timeout: float = 600.0) -> str:
    """Generate locally. Nothing leaves the machine."""
    import httpx

    resp = httpx.post(
        f"{OLLAMA}/api/generate",
        json={"model": model, "prompt": prompt, "stream": False},
        timeout=timeout,
    )
    if resp.status_code == 404:
        installed = ollama_models()
        raise RuntimeError(
            f"Ollama has no model called '{model}'."
            + (f" Installed: {', '.join(installed)}" if installed
               else " No models installed — try `ollama pull llama3.1`.")
        )
    resp.raise_for_status()
    return json.loads(resp.text)["response"]


# ------------------------------------------------------ hand off to a terminal

TAILOR_DIR = db.DATA_DIR / "tailor"


def save_prompt(prompt: str, ctx: dict, position: int) -> Path:
    """Write the prompt where both a terminal and the user can reach it."""
    TAILOR_DIR.mkdir(parents=True, exist_ok=True)
    slug = db.slug(f"{ctx['company']} {ctx['title']}")[:60]
    path = TAILOR_DIR / f"{position:03d}-{slug}.md"
    path.write_text(prompt, encoding="utf-8")
    return path


def _command_for(prompt_path: Path, ctx: dict, tool: str, model: str,
                 windows: bool) -> str:
    """The line that actually invokes the model."""
    if tool == "ollama":
        if ctx.get("position") == 0:
            # Advice has no posting to index into, so the prompt file is piped.
            body = (f"(Get-Content -Raw -LiteralPath {_ps(str(prompt_path))})" if windows
                    else f'"$(cat {_sh(str(prompt_path))})"')
            return f"ollama run {model or 'llama3.1'} {body}"
        return f"uv run autowork tailor {ctx['position']} --ollama {model or 'llama3.1'}"
    if windows:
        return f"claude (Get-Content -Raw -LiteralPath {_ps(str(prompt_path))})"
    return f'claude "$(cat {_sh(str(prompt_path))})"'


def runner_script(prompt_path: Path, ctx: dict, tool: str, model: str = "",
                  command_override: str | None = None) -> Path:
    """A script that shows what it is about to do, then waits.

    Deliberately not launched straight into the model. The terminal opens with
    everything staged and stops at a prompt, so the run is one keypress away but
    still a decision — an agent that starts rewriting your resume because a page
    was clicked is not what anyone asked for.

    PowerShell on Windows, /bin/sh everywhere else. The original wrote a shell
    script on every platform and handed it to `cmd /k`, which cannot run
    `read`, `exec` or `$(cat ...)` — the Windows path was broken outright.
    """
    import sys

    TAILOR_DIR.mkdir(parents=True, exist_ok=True)
    windows = sys.platform.startswith("win")
    command = command_override or _command_for(prompt_path, ctx, tool, model, windows)

    lines = [
        ctx["title"], ctx["company"],
        f"resume:  {ctx['resume']}", f"prompt:  {prompt_path}",
    ]
    if ctx.get("missing"):
        lines.append(f"not evidenced: {', '.join(ctx['missing'][:8])}")
    lines.append("Press Enter to run, or Ctrl-C to back out.")

    # Named per posting: a single shared runner means clicking tailor on two
    # roles in quick succession has the second overwrite the first before the
    # terminal has read it.
    script = TAILOR_DIR / f"run-{ctx['position']:03d}.{'ps1' if windows else 'sh'}"

    if windows:
        body = [f"Set-Location -LiteralPath {_ps(str(db.REPO_ROOT))}", "Clear-Host"]
        body += [f"Write-Host {_ps('  ' + line)}" for line in lines]
        body += ["Read-Host | Out-Null", command]
        script.write_text("\r\n".join(body) + "\r\n", encoding="utf-8")
        return script

    body = ["#!/bin/sh", f"cd {_sh(str(db.REPO_ROOT))} || exit 1", "clear"]
    body += [f"echo '  {_esc(line)}'" for line in lines]
    body += ["read _", f"exec {command}"]
    script.write_text("\n".join(body) + "\n", encoding="utf-8")
    script.chmod(0o755)
    return script


def _sh(value: str) -> str:
    """Single-quote for /bin/sh."""
    return "'" + value.replace("'", "'\\''") + "'"


def _ps(value: str) -> str:
    """Single-quote for PowerShell, where a literal quote is doubled."""
    return "'" + str(value).replace("'", "''") + "'"


def _esc(value: str) -> str:
    """Safe inside a single-quoted sh echo."""
    return str(value).replace("'", "")


def launch_terminal(script: Path) -> tuple[bool, str]:
    """Open a terminal running `script`. Returns (launched, detail)."""
    import os
    import shutil
    import subprocess
    import sys

    target = str(script)
    if sys.platform == "darwin":
        return _launch_macos(target)
    if sys.platform.startswith("win"):
        return _launch_windows(target)
    # $TERMINAL first: someone who set it has an opinion. x-terminal-emulator
    # is Debian's alternatives symlink, so it covers most desktops after that.
    candidates = [(os.environ["TERMINAL"], ["-e"])] if os.environ.get("TERMINAL") else []
    candidates += [
        ("x-terminal-emulator", ["-e"]), ("gnome-terminal", ["--"]),
        ("konsole", ["-e"]), ("xfce4-terminal", ["-e"]), ("alacritty", ["-e"]),
        ("kitty", []), ("xterm", ["-e"]),
    ]
    for term, args in candidates:
        if not shutil.which(term):
            continue
        try:
            subprocess.Popen([term, *args, "/bin/sh", target])
        except OSError:
            continue          # installed but unlaunchable — try the next one
        return True, term
    return False, "no terminal emulator found — run the script yourself"


def _launch_windows(target: str) -> tuple[bool, str]:
    """Open a new PowerShell console on the script.

    The original branch ran `cmd /c start cmd /k <script.sh>`, which was broken
    twice over: cmd cannot execute a `#!/bin/sh` script, and it reported
    success unconditionally, so the page said a terminal had opened when none
    had. `runner_script` now emits PowerShell here.

    `-ExecutionPolicy Bypass` because the script is generated locally and
    unsigned, and the default policy on Windows client editions refuses it.
    `-NoExit` keeps the window up after the model exits, so the output is still
    readable.
    """
    import shutil
    import subprocess

    shell = shutil.which("pwsh") or shutil.which("powershell")
    if not shell:
        return False, "no PowerShell found on PATH"
    args = [shell, "-NoLogo", "-NoExit", "-ExecutionPolicy", "Bypass", "-File", target]

    # Windows Terminal, when present, opens its own tab and needs no console
    # flag; plain conhost needs CREATE_NEW_CONSOLE or it would inherit this
    # process's console and never appear.
    if terminal := shutil.which("wt"):
        args = [terminal, *args]
        flags = 0
    else:
        flags = getattr(subprocess, "CREATE_NEW_CONSOLE", 0)

    try:
        subprocess.Popen(args, creationflags=flags)
    except OSError as exc:
        return False, f"could not start PowerShell: {exc}"
    return True, "Windows Terminal" if "wt" in args[0] else "PowerShell"


def _launch_macos(target: str) -> tuple[bool, str]:
    """Open Terminal.app on the script, waiting for the shell to be ready.

    Two earlier attempts failed, and both failures were about the same thing —
    `do script` types into an interactive login shell, so anything the shell
    does at startup competes for that input.

    1. `open -a Terminal <script>` types the path and appends `; exit;`. With
       oh-my-zsh's "Would you like to update? [Y/n]" waiting, the prompt ate the
       leading `/` and zsh got a relative path that did not exist.
    2. Opening an empty window first answers that prompt — with its default,
       which is *yes* — and a fixed delay then expired midway through the
       update, so the real command was swallowed by the updater's stdin.

    Terminal exposes `busy` per tab, so poll it instead of guessing: the
    command is only sent once the shell is actually idle at a prompt.
    """
    import subprocess

    applescript = f'''
    tell application "Terminal"
      activate
      set shellTab to do script ""
      set waited to 0
      repeat while busy of shellTab and waited < 120
        delay 0.5
        set waited to waited + 1
      end repeat
      if busy of shellTab then return "busy"
      do script {_osa(f"exec {_sh(target)}")} in shellTab
      return "ok"
    end tell
    '''
    result = subprocess.run(["osascript", "-e", applescript],
                            capture_output=True, text=True)
    if result.returncode != 0:
        # Most likely the one-time "allow Terminal automation" dialog was
        # declined. Say so rather than reporting a silent success.
        return False, (result.stderr.strip().splitlines() or ["osascript failed"])[-1]
    if result.stdout.strip() == "busy":
        return False, "the new shell never reached a prompt (still starting up?)"
    return True, "Terminal"


def _osa(value: str) -> str:
    """Quote for an AppleScript string literal."""
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'


def claude_available() -> bool:
    import shutil

    return shutil.which("claude") is not None


ADVICE_PROMPT = """\
Help me decide what jobs to target. Then, if I agree, edit my settings for me.

## My resume(s)

{resumes}

## What the tool currently has

Target city: {city}
Experience band: {band}
Role families selected: {families}
Current target titles: {titles}
Skills currently starred as most important: {starred}

## How the matching works

Postings are gated first: the role family decides which job titles count at
all, then seniority and location. What survives is scored on how well the title
matches my target titles and how many of my starred skills the posting asks
for. Starred skills weigh most.

## What I want from you

1. **Target titles.** What should I actually be searching for, given what the
   resume evidences rather than what I might like? Say which of my current ones
   are too broad, too narrow, or aspirational, and what to add.
2. **Skills to star.** Which 5-8 of my skills should carry the most weight?
   Pick for what I want to be hired to do next, not for what appears most.
3. **Role families.** Are the selected ones right? Should another be added?
4. **Honest read.** What am I underselling, and what is a stretch?

Be specific and short. Argue for your picks; do not just agree with mine.

Then offer to apply the changes by editing `profile/profiles.json` directly —
`target_titles` and `skills` under each profile, and `constraints.role_families`
— but only after I say yes. Keep a note of what you changed.
"""


def advice_prompt(resumes: list[dict], answers: dict) -> str:
    """Prompt for the "what should I target?" conversation."""
    blocks = []
    for resume in resumes:
        text = (resume.get("text") or "").strip()
        blocks.append(
            f"### {resume.get('label') or 'Resume'}\n\n"
            + (text[:9000] if text else
               "(not re-uploaded this session — skills: "
               + ", ".join(resume.get("skills") or []) + ")")
        )
    return ADVICE_PROMPT.format(
        resumes="\n\n".join(blocks) or "(none provided)",
        city=answers.get("city") or "not set",
        band=answers.get("experience_band") or "not set",
        families=", ".join(answers.get("families") or []) or "not set",
        titles=", ".join(
            t for r in resumes for t in (r.get("roles") or [])) or "none",
        starred=", ".join(
            s for r in resumes for s in (r.get("key_skills") or [])) or "none",
    )


FOLLOWUP_PROMPT = """\
Draft a short follow-up email about a job application.

## The application

Company: {company}
Role: {title}
Applied: {days} days ago
Posting: {url}

## The thread you are replying into

From: {sender}
Subject: {subject}
Sent: {date}
{status}

## My resume

{resume}

## What to write

A reply to that thread. Rules, in order of importance:

1. **Short.** Four sentences at most. A recruiter reads it on a phone between
   meetings, and length reads as anxiety.
2. **Give them a reason to reply**, not just a nudge. One specific, concrete
   thing from my background that maps to what this role needs, in one line.
   Not a summary of the resume — one thing.
3. **No pressure and no apology.** Not "just checking in", not "sorry to
   bother". Say what you want: an update on where the application stands.
4. **Nothing invented.** Every claim traces to the resume above. If nothing
   maps cleanly, say less rather than reaching.

Output the subject line and the body, nothing else. Do not send it — I will
read it first.
"""


def followup_prompt(app: dict, message, resume_text: str) -> str:
    """Prompt for a follow-up reply into an existing thread."""
    status = ("They have replied at least once, so this continues a real "
              "conversation." if message and not message.automated else
              "This is the automated acknowledgement — no human has replied yet.")
    return FOLLOWUP_PROMPT.format(
        company=app.get("company", ""), title=app.get("title", ""),
        days=app.get("days", "?"), url=app.get("url", ""),
        sender=(message.sender if message else "unknown"),
        subject=(message.subject if message else ""),
        date=(message.date if message else ""),
        status=status, resume=resume_text[:9000],
    )
