# Setting up AutoWork

A job search that runs itself. Every morning it reads the careers pages of ~250
companies, keeps the roles that actually fit you, and ranks them against your
resume. You review them in a web page on your own laptop.

**It is free.** No accounts, no subscriptions, no API keys. Nothing is uploaded
anywhere — your resume is read on your own machine and never leaves it.

You need about **10 minutes**, and you need to be comfortable typing a few
commands. That is the only hard requirement.

---

## Step 1 — Install the two things it needs

**Git**, to download the project.

- **Windows** — [git-scm.com/download/win](https://git-scm.com/download/win),
  run the installer, accept the defaults.
- **macOS** — open Terminal and run `git --version`. If it asks to install
  developer tools, say yes. That is all it takes.

**uv**, which handles Python for you. You do not need to install Python
separately — uv fetches the right version itself.

- **Windows** — open **PowerShell** and paste:

  ```powershell
  powershell -ExecutionPolicy ByPass -c "irm https://astral.sh/uv/install.ps1 | iex"
  ```

- **macOS** — open **Terminal** and paste:

  ```sh
  curl -LsSf https://astral.sh/uv/install.sh | sh
  ```

Then **close the window and open a new one** — this matters, the new command
is not available until you do. Check it worked:

```
uv --version
```

If you see a version number, you are set. If you see "command not found",
reopen the terminal once more.

---

## Step 2 — Download the project

In that same window:

```
git clone https://github.com/balajidnz/AutoWork.git
cd AutoWork
uv sync
```

> If that fails with "repository not found", the project is private — ask
> whoever sent you here to add you, or to make it public.

`uv sync` downloads what the project needs. First time takes a minute or two.

> **Where am I?** Every command from here on is typed in this same window,
> inside the `AutoWork` folder. If you close it, reopen it and `cd` back in.

Nobody's personal data comes with the download — no profile, no resumes, no
application history. What you do get is the list of 250+ company career pages,
which takes hours to build and is the same for everyone. (If you ever want to
wipe your own setup and start over: `uv run autowork reset`.)

---

## Step 3 — Set up your profile

```
uv run autowork console
```

Your browser opens on a setup page. Drag your resume onto it — PDF works fine.

**Add more than one if you have them.** If you have a version tuned for
different kinds of role, add each; every job gets scored against all of them,
and each listing tells you which one to send.

It reads the resume and fills in the form for you. **Check what it guessed** —
it is a guess, and a wrong one quietly skews everything:

- **Your name and city** — usually right.
- **Experience** — read from your dates. Correct it if it is off.
- **Roles you want** — remove any that are not really you, add any missing.
- **What kind of roles** — engineering, design, product, data, marketing,
  sales, operations, or "anything". Pick more than one if you are open to
  both. Whatever you leave off gets filtered out of your results.
- **Star the skills you want to be hired for** — the one worth thinking about.
  It counts how often each skill appears, but that measures what your resume
  *talks about*, not what you want to do next. One real resume mentioned Redis
  six times and Kubernetes twice, and the person wanted Kubernetes work.

Not sure what to target? There are **Ask Claude Code** / **Ask Ollama** buttons
that open a terminal and let a model argue it out with you — only if you have
one of those installed. Skip it otherwise; you can change everything later.

Press **Save**.

---

## Step 4 — Find the jobs

Back in your terminal:

```
uv run autowork poll
uv run autowork rank
```

`poll` reads every company's careers page — a minute or two. `rank` scores them
against your resume, which is quick.

Then reload the browser page. That is your shortlist.

---

## Using it

Run `uv run autowork console` whenever you want it.

One line per job, best first. Click one to see why it ranked there, which of
its requirements your resume does not evidence, and what to do about it.

- **Arrow keys** move, **Enter** expands, **O** opens the posting.
- **A** marks it applied, **S** saves it, **X** hides it.
- **⚙ Your profile & resumes** at the top right, to change anything.

Your decisions are saved. Close it and come back whenever.

To refresh with new postings, run `poll` and `rank` again. Once a day is plenty.

---

## Optional: rewrite your resume for a specific job

Each job has **Tailor in Claude Code** and **Tailor with Ollama**. Either opens
a terminal with everything set up and waits for you to press Enter.

You need one of these installed:

- **[Claude Code](https://claude.com/claude-code)** — best results, needs a
  Claude subscription.
- **[Ollama](https://ollama.com)** — free and runs on your own machine.
  Install it, then `ollama pull llama3.1`.

Neither? `uv run autowork tailor 3` (where 3 is the number next to a job)
prints the text to paste into any chatbot.

Whichever you use, it is told never to invent anything that is not already on
your resume. A resume that wins a screen on a skill you do not have loses the
interview.

---

## Optional: get the list sent to you

Instead of opening the page, have it arrive by email or Telegram. Both are
free, and you can use either or both.

Create a file called **`.env`** in the `AutoWork` folder. It is ignored by git,
so nothing in it is ever committed or shared.

### Email (Gmail)

You need a **Google app password** — not your normal password, which Google
will reject.

1. Turn on 2-Step Verification: **myaccount.google.com/security**. App
   passwords do not exist until you do; if the page says "the setting you are
   looking for is not available", this is why.
2. Go to **myaccount.google.com/apppasswords**, type any name (e.g. `AutoWork`)
   and create it.
3. Copy the 16 characters it shows, **with the spaces removed**.

Put this in `.env`:

```
SMTP_USER=you@gmail.com
SMTP_PASS=abcdefghijklmnop
DIGEST_TO=you@gmail.com
```

### Telegram

1. In Telegram, message **@BotFather**, send `/newbot`, and follow it. It gives
   you a token.
2. **Send your new bot any message** — it cannot find you until you do.
3. Then run:

   ```
   uv run autowork telegram-setup --token PASTE_YOUR_TOKEN
   ```

   It prints your chat ID. Add both to `.env`:

```
TELEGRAM_BOT_TOKEN=123456:ABC-DEF...
TELEGRAM_CHAT_ID=987654321
```

### Check it, then send

```
uv run autowork doctor          # says what it can see; never prints the values
uv run autowork digest --dry-run    # builds it, sends nothing
uv run autowork digest              # actually sends
```

If the email is rejected, `doctor` will tell you why — an app password is
exactly 16 characters with no spaces, and that is the usual mistake.

### Every morning, without you

To have it run on its own, use GitHub Actions — free, and it needs no machine
of yours to be switched on. It is a bit more setup; see the **Scheduling**
section of [README.md](README.md).

---

## When something goes wrong

**`uv: command not found`**
Close the terminal and open a new one. If it still fails, the installer did not
finish — run it again and read its last few lines.

**`git: command not found`** (Windows)
Use **Git Bash**, which the Git installer added to your Start menu, rather than
PowerShell.

**The browser did not open**
Go to **http://localhost:8765** yourself. If nothing is there, the command is
not running — check the terminal for a red error.

**"No jobs collected yet"**
You have not run `poll` and `rank`. Step 4.

**The shortlist is empty, or tiny**
Usually the profile is too narrow. Open **⚙ Your profile & resumes** and:
- add more target roles, or broader ones;
- tick more than one role family, or "Anything";
- untick "Only show jobs in my city";
- widen your experience band.

Then run `uv run autowork rank` again — you do not need to re-poll.

**Everything is in the wrong field**
You picked the wrong role family. Change it in the profile page and re-run
`rank`.

**Windows: "running scripts is disabled on this system"**
PowerShell blocks unsigned scripts by default. Run this once, in the same
window, then try again:

```powershell
Set-ExecutionPolicy -Scope Process -ExecutionPolicy Bypass
```

**macOS: it asks for permission to control Terminal**
That is the tailoring button opening a terminal window for you. Allow it, or
skip that feature — nothing else needs it.

---

## What it does with your data

Everything stays on your laptop.

- The file you upload is never saved. Its **text** is, as
  `profile/resume-<name>.md` — the tailoring feature needs the words, not the
  PDF. It stays in your project folder.
- The web page is served to your machine only (`127.0.0.1`) — nobody on your
  network can reach it.
- Nothing is sent anywhere except the public careers pages it reads jobs from.
- If you set up the optional morning email, that goes through your own account.
