# AutoWork

Daily job sourcing that runs on free tiers only. Polls company applicant
tracking systems directly rather than scraping job boards, ranks the results
against your profile, and emails a shortlist every weekday morning.

The design goal is applying into **short queues**. A posting on a company's own
Greenhouse board that has not yet been syndicated to LinkedIn or Naukri has far
fewer applicants than the same posting three days later, so the pipeline
optimises for reaching postings early and directly.

## Status

| Phase | What | State |
|---|---|---|
| 1 | ATS pollers, watchlist, dedup, ranking, digest email | **done** — running at 07:00 IST weekdays |
| 2 | Web review console + setup wizard | **done** — `autowork console` |
| 3 | Resume tailoring — Claude Code, Ollama, or prompt-only | **done** |
| 3.5 | Comp estimates, keyword search, application tracking | **done** |
| 3.6 | Resume renderer, contact discovery, Telegram helper | **done** |
| 4 | Hermes MCP wrapper | not started |

254 verified boards across Greenhouse, Lever, Ashby and SmartRecruiters, plus
LinkedIn keyword search; ~24,000 postings per run, pruned to the ~7,000 still
open; **62 core + 38 stretch** eligible across three resume tracks — infra,
product and agentic, with a median age of 14 days.

## Cost

Nothing in the stack has a billing meter. Greenhouse, Lever and Ashby publish
unauthenticated JSON board APIs; GitHub Actions, Gmail SMTP and SQLite are free
at this volume. The LLM layer is Claude Code running on an existing Max
subscription, invoked locally on demand — there is no API key anywhere.

## Quick start

```sh
uv sync
uv run autowork console    # opens the setup wizard: drop in your resumes
```

Then collect the jobs:

```sh
uv run autowork verify     # resolve data/companies.txt -> ATS board tokens
uv run autowork poll       # fetch every verified board
uv run autowork stats      # what landed
```

`verify` currently resolves ~53% of a hand-written company list by guessing
board tokens from company names and confirming each against the live API. A
wrong guess costs one HTTP request; a missing company costs jobs, so the seed
list in `data/companies.txt` is meant to be over-inclusive.

## How the pieces fit

```
data/companies.txt ──verify──> boards table ──poll──> jobs table
                                                         │
                                    rank (rules + BM25) ─┤
                                                         ▼
                              digest email + data/digest/YYYY-MM-DD.jsonl
```

**Sources.** Each adapter in `autowork/sources/` exposes
`fetch(client, token) -> list[Job]` and raises `BoardNotFound` for a token that
does not exist, which is what the verifier keys off. Adding an ATS is one file.

**Dedup.** A `dedup_key` of normalised company + title identifies the same
opening across sources. Location is deliberately excluded, because the same
role appears as "Bengaluru", "Bangalore, India" and "Remote - India" on
different boards.

**The early signal.** `sightings` records which sources saw each opening.
An opening seen only from ATS sources has not reached the aggregators yet — the
stand-in for applicant count, and the reason ATS boards are primary rather than
supplementary. This was vacuous while ATS boards were the only source, since
everything was ATS-only by definition; adding LinkedIn search made it real.

## The digest

```sh
uv run autowork digest --dry-run   # workbook + HTML preview, sends nothing
uv run autowork digest             # sends, then records what was sent
```

Two sections, answering different questions. **New since last digest** is what
you act on today. **Standing shortlist** is everything still eligible that you
have not marked applied or skipped, so nothing falls quietly off the bottom
while you were busy.

The email is multipart — plain text, an HTML table with each role linked
directly to its apply URL, and the full ranked workbook attached. The workbook
has a sheet per section, frozen headers, an autofilter, and the apply URL as a
cell hyperlink rather than a 120-character string in a column.

Delivery is Gmail SMTP over STARTTLS. Credentials come from `SMTP_USER`,
`SMTP_PASS` (a Google app password, not your account password) and
`DIGEST_TO` — environment only, never the repo.

**The ledger is written after a successful send, not before.** A failed send
would otherwise burn the day's postings: they would be marked as shown, and
never appear in another digest. `--dry-run` never touches it.

## Scheduling

`.github/workflows/digest.yml` runs at 01:30 UTC (07:00 IST) on weekdays, then
commits the ledger and the day's digest back to the repo. `workflow_dispatch`
gives a manual trigger with a dry-run toggle.

Repository secrets:

| Secret | Required | What it is |
| --- | --- | --- |
| `PROFILE_JSON` | yes | The whole of `profile/profiles.json`. It is gitignored — it carries a salary and target titles — so the run has nothing to rank against without it. |
| `SMTP_USER` / `SMTP_PASS` | for email | Gmail address and its 16-character app password. `SMTP_PASSWORD` is accepted too. |
| `DIGEST_TO` | for email | Where the digest is sent. |
| `TELEGRAM_BOT_TOKEN` / `TELEGRAM_CHAT_ID` | for Telegram | From `autowork telegram-setup`. |

`PROFILE_JSON` holds a file, not a path — set it by redirecting the file in, and
check it parses first, because the value cannot be read back out of GitHub
afterwards to see what landed there:

```sh
uv run python -m json.tool profile/profiles.json > /dev/null   # fails loudly if malformed
gh secret set PROFILE_JSON < profile/profiles.json
```

Re-set it whenever you change your profile — the wizard writes your laptop's
copy, and the scheduled run only ever sees the secret.

## Companies that run their own portal

`verify` resolves a company only if it uses Greenhouse, Lever, Ashby or
SmartRecruiters. Probed against all four, Amazon, Netflix, Google, Microsoft,
Apple and Meta resolve to nothing — they run their own careers sites. Airbnb
was the one exception and is now on the watchlist.

Of those portals, Amazon's answers an unauthenticated GET with JSON, so it has
an adapter in `autowork/sources/amazon.py`:

```
GET https://www.amazon.jobs/en/search.json?normalized_country_code[]=IND&base_query=...
```

`normalized_country_code[]` is the filter that works; `country[]` and
`loc_query` both return 10,000 hits led by Kuala Lumpur and Sunnyvale.

Two things it taught the rest of the pipeline. The portal reports **legal
entities** — "ADCI - Karnataka", "ADCI HYD 13 SEZ", "ADSIPL - Telangana" —
which split one employer across the per-company cap and the
duplicate-application guard, so the adapter normalises them to "Amazon" and
keeps the entity in `department`. And 454 Amazon postings put **136 of 249
shortlist rows under one company**, which is why `shortlist` now caps each
employer at `signals.max_per_company`.

Worth saying plainly: a big-company posting is the opposite of this project's
premise. It is here because Amazon India hires SDE-1s in volume, not because it
fits the thesis.

## Review console

```sh
uv run autowork console          # http://localhost:8765
```

A single HTML page served by `http.server` from the standard library. No build
step, no npm, no CDN — it works offline, and there is no framework version to
keep alive for a page one person opens on their own laptop. It replaced a
Streamlit console that pulled in pandas and pyarrow to render a list.

One collapsed row per role: rank, title, company, location, age, match
strength. Expanding one shows why it ranked there, which requirements your
resume does not evidence, the pay estimate, and the buttons. Triage is
keyboard-driven — arrow keys to move, `enter` to expand, `o` to open the posting,
`a`/`s`/`x` to mark applied, saved or skipped, `/` to search.

**⚙ Your profile & resumes** in the header reopens the wizard on the saved
config, to add or drop a resume, re-star skills, or move city. There is no
sign-in: it is one person on their own machine, and the profile lives in
`profile/profiles.json`.

The rank number on a row is the number `/tailor` and `autowork show` resolve.
That number indexes the *unfiltered* shortlist, deliberately: if it were the
position on screen, searching or re-sorting would point `/tailor 3` at a
different job than the row showing 3.

The screen shares no vocabulary with the pipeline. The ranker thinks in tiers,
tracks, coverage ratios and a score on an unstated scale; `present.py`
translates each of those at the edge, and `tests/test_console.py` fails if
`rank.py` grows a reason with no plain-language wording.

It carries a **duplicate-application guard**: two resumes surface the same
employer on different tracks, and sending one recruiter two divergent CVs
through a single ATS reads badly, so applying to a company flags every other
role there.

Decisions persist to `data/status.json` rather than the database, which is
rebuilt from the boards on every poll.

**Freshness is a multiplier, not a bonus.** It used to add at most 10 points,
which a strong skill match simply outweighed: on a live shortlist the median
role was 22 days old and a 37-day-old posting ranked second. Score is now
multiplied by `0.5 ** (age / freshness_half_life_days)`, and undated rows fall
back to `first_seen` — LinkedIn returns no post date, and treating those as
ageless let them bypass the staleness gate and then top the list.

**`autowork prune`** deletes postings the board no longer lists or that are
past `max_age_days`, keeping anything you have marked. First run on a corpus in
daily use: 24,317 rows to 7,134, and the database from 184MB to 56MB.

The console shows how old its corpus is and offers to refresh it. The scheduled
run polls on a GitHub runner and discards that database, so a laptop that never
polls shows the same list indefinitely — measured at 13 days, with nothing on
screen saying so.

## Role families — using this outside engineering

Everything about "is this the right kind of job" used to be hard-coded to
engineering: a title allowlist, plus a block list containing `designer`,
`marketing`, `product manager` and `data analyst`. A designer running this
matched **nothing** — rejected by the allowlist, then rejected again by the
blocks.

The family is now configuration. `autowork/profile_template.json` ships eight —
engineering, design, product, data, marketing, sales, operations, and `any` —
each naming the titles it wants and the neighbouring fields it does not. Pick
one or several in the setup wizard; the ones you leave off become the
blocklist.

Across several families the blocklist is the **intersection**, not the union: a
term is only wrong if every family you picked rejects it. Union would mean
someone open to engineering *and* data blocks "Data Engineer" — a job squarely
inside both.

`any` disables the allowlist entirely, for a field the shipped list does not
name. Matching then rests on target titles and skills alone. That is looser,
but returning nothing at all is not a better answer.

Measured on the same 20,000-posting corpus: engineering 82 postings clear the
gates, design 6, data 5, marketing 6, `any` 188. The watchlist is tech
companies, so the non-engineering roles are the ones tech companies advertise —
worth knowing before pointing a friend in finance at it.

When more than one family is selected, the console gets a field filter so you
can narrow to one while browsing.

## Setting up for someone else

`autowork console` opens a setup wizard when there is no profile yet. Drop in
one resume or several — an infrastructure resume and a product resume target
different jobs, and every posting is scored against each separately. It reads
the skills, guesses the roles, experience and city, and shows all of it back
for correction.

**Not sure what to target?** The wizard has *Ask Claude Code* and *Ask Ollama*
buttons that open a terminal with your resume and current settings, and ask the
model to argue for the titles and skills worth targeting — then edit
`profile/profiles.json` for you if you agree. Same staged handoff as tailoring:
it stops at `Press Enter`.

One question there is worth answering carefully: **which skills you want to be
hired for**. Frequency is the only signal a document gives, and it measures
what a resume talks about rather than what its owner wants to do next — a real
infrastructure resume here says Redis six times and Kubernetes twice. Starred
skills are pinned above whatever the parser counted.

The config it writes has two halves. `autowork/profile_template.json` holds the
gates that describe the Indian entry-level engineering market — which titles
are not engineering, which level tokens mean senior, which "remote" postings
are really scoped abroad. Those took a 20,000-posting corpus to calibrate and
are the same for everyone. The other half is you, and comes from the wizard.
An existing config is backed up before it is replaced.

Nothing leaves the machine: the resume is parsed in memory, never written to
disk, and the server binds to `127.0.0.1` only.

## Tailoring a resume

Every expanded role has **Tailor in Claude Code** and **Tailor with Ollama**.
Either one stages the prompt and opens a terminal showing the role, the resume
it picked, and what the posting asks for that the resume does not evidence —
then stops at `Press Enter to run`. Staged, not started: a button click should
not silently set an agent rewriting your resume.

On macOS this drives Terminal.app over AppleScript and waits for the new shell
to report itself idle before sending anything. `open -a Terminal <script>`
looks simpler but types the path into a login shell, and any startup prompt —
oh-my-zsh'''s update check, for one — eats the first character. macOS will ask
once for permission to control Terminal.

From the shell, the same thing three ways:

```sh
/tailor 3                                     # Claude Code slash command
uv run autowork tailor 3 --ollama llama3.1    # local model, nothing leaves the machine
uv run autowork tailor 3                      # prints the prompt to paste anywhere
```

The prompt carries the posting, the resume, and the requirements the resume
does not currently evidence. Every claim in the output must trace to a line
already in the resume: a resume that wins a screen on an invented skill loses
the interview, which is worse than not getting the screen.

### Why the writes need a token

The console can spawn a terminal, and any page you visit can POST to
`localhost` from your browser. So the server mints a random token per run,
injects it into the page, and refuses any write that does not carry it — plus a
same-origin check. Reads are open; writes are not.

## Requirement coverage

`autowork/coverage.py` diffs what a description asks for against what the
resumes evidence, and reports the gap (`62% — missing: Java, Azure, Linux`) in
the console, in `autowork top --coverage`, and as a workbook column.

This deliberately models the **literal keyword filter** an ATS runs before a
human sees anything — not the judgement a screener applies afterwards. The two
fail differently: the ATS drops you for missing the exact string, a screener for
thin evidence. Only the second needs an LLM. Terms carry their surface forms,
since "Kubernetes" and "K8s" are one skill to a reader and two strings to a
search box, and scanning is biased to the requirements section so company
boilerplate is not mistaken for a requirement.

## Keyword search (JobSpy)

Board polling only sees companies on the watchlist. `autowork/sources/search.py`
adds keyword search through [JobSpy](https://github.com/speedyapply/JobSpy),
configured under `job_search` in `profiles.json`.

Of JobSpy's eight boards, **only two return anything for India**, measured
rather than assumed:

| Board | Result |
|---|---|
| LinkedIn | works — clean locations, full descriptions, structured `job_level` |
| Indeed | works, but noisier and rate-limits after ~8 queries from one IP |
| Naukri | `406 recaptcha required` |
| Glassdoor / Google / ZipRecruiter / Bayt | zero rows for Bangalore |

Only LinkedIn is enabled by default. It is scraped **unauthenticated** — no
cookie, no session, nothing tied to an account, which is a different thing from
automating actions as a logged-in user.

It contributes **22 of 58 eligible roles** (SAP, Amagi, Lowe's India, Microsoft,
SolarWinds, Couchbase, Cargill), and it is what finally makes the ATS-only
signal mean something: previously every posting was ATS-only by definition, and
now 5 are confirmed on both an ATS and an aggregator.

**Seniority now comes from the source where the source states it.** LinkedIn's
`job_level` and SmartRecruiters' `experienceLevel` are normalised onto one
vocabulary in `level_hint` and checked before the title regex — a grading the
board did itself beats one inferred from a string.

The cost is runtime: LinkedIn fetches each description as a separate request, so
the poll goes from ~20s to several minutes. Descriptions are not optional, since
the experience bar lives in them, so the lever is `results_per_term`.

## Application tracking

```sh
uv run autowork track                              # pipeline + follow-ups due
uv run autowork track --set <job-id> screening     # advance one application
```

Sourcing ends at the apply button; this is the other half. `status.json` gains a
pipeline — `shortlisted → applied → screening → interview → offer / rejected` —
and the console's buttons follow it, so an applied role offers *screening* and
*rejected* rather than *applied* again.

**Follow-ups** surface at the top of the digest, above the new roles, because
they are the only part with a deadline: a new posting keeps, a follow-up window
closes. The window is 7 to 30 days after applying — long enough that silence
means something, short enough that the role is open and the recruiter still
recognises the name. Past 30 days an application is marked *gone cold* rather
than nudged again.

Only `applied` qualifies. Once a conversation has started, silence is the
recruiter's process rather than a dropped thread, and chasing it on a timer is
the wrong instinct.

The digest also carries a pipeline line — live applications, applied this week,
response rate. The rate only counts applications old enough to have plausibly
been answered; including yesterday's would drag it down for no reason.

## Resume rendering

```sh
uv run autowork render profile/tailored/swiggy-sde-1.md
```

`/tailor` produces markdown and no ATS accepts a markdown file, so without this
the tailoring loop stops one step short of something uploadable. `/tailor` now
calls it automatically.

Auto-shrinks the body size until the resume fits one page, stopping at 7pt — a
tailored resume varies in length with the role, and two pages at SDE-1 reads
worse than one tight page. If it lands below 8pt the content is too long and the
fix is to cut a bullet, not to ship unreadable type.

Three things the implementation had to work around:

- **WeasyPrint needs pango and cairo** as system libraries and does not install
  cleanly on macOS, so layout is hand-built on fpdf2, which is pure Python.
- **fpdf2's core fonts are latin-1.** Em-dash, en-dash, `₹` and curly quotes all
  *raise* rather than degrade, and every one appears in these resumes, so they
  are transliterated first.
- **`cell()` line-breaks on its own** when a token crosses the right margin, and
  that break resets x to the page margin — silently overriding the hanging
  indent and re-wrapping text already measured. Lines are wrapped here and
  placed absolutely with `text()` instead.

## Contact discovery

```sh
uv run autowork contacts
uv run autowork contacts --company Swiggy --name "Priya Sharma"
```

Scoped narrowly on purpose. There is no free, reliable way to discover a named
hiring manager's mailbox, and a tool that guesses one and presents it as fact is
worse than none. So it does three honest things: extracts addresses the posting
actually contains, resolves the employer's mail domain and confirms it accepts
mail, and — given a name you found elsewhere — offers the address forms that
domain most likely uses, labelled unverified.

Addresses are split into **person** and **queue**. `careers@` is a real inbox,
but writing to it is just the application again; reporting both in one list
would make the second look as valuable as the first.

Measured on the current shortlist: **0 of 39 postings name a person**, 4 expose
a careers queue, 31 have a resolved mail domain. That is the honest ceiling on
this feature — the useful half is turning a name you find on LinkedIn into an
address without also guessing the format.

Mailbox verification stops at the domain. Probing an individual mailbox over
SMTP is what paid verifiers do, and it is both widely blocked and rude.

## Compensation estimates

```sh
uv run autowork comp     # populate data/comp.json for the shortlist
```

Indian postings essentially never publish salary — measured at **0/40** on both
Indeed and LinkedIn — so the pay question cannot be answered from the listing at
all. `autowork/comp.py` reads AmbitionBox's `OccupationAggregationByEmployer`
JSON-LD, which gives percentiles, median, sample size and the experience band
the sample covers.

Estimates appear as an **Est. comp** workbook column, in the email under each
role, and in the console — coloured red and labelled `BELOW your ₹17L` when the
median falls under the floor in `profiles.json`. That flag is the whole point:
it caught a Bangalore `SDE I` at ₹13L median that had otherwise ranked well.

Three things the implementation is careful about:

- **Cached for 90 days** in `data/comp.json`, misses included. Company pay bands
  move slowly, and a company with no AmbitionBox page will never have one, so
  re-requesting it every morning is wasted traffic on someone else's server.
- **Throttled**, and deliberately so. With company and role fallbacks a cold
  cache is a few hundred requests; firing them back-to-back got the whole batch
  soft-blocked and returned zero hits on the first attempt.
- **Two fallbacks**, because the obvious URL often misses: the specific role
  404s where the generic one resolves (GitLab has `software-engineer`, not
  `backend-developer`), and Lever and Ashby do not echo a display name, so the
  company arrives as its board token — `hevodata` rather than `hevo-data`.

Sample sizes under 15 are labelled `thin sample`. These are self-reported CTC
figures, not offers.

## Tests

```sh
uv sync --group dev
uv run pytest tests/ -q
```

128 cases, ~0.1s. Weighted almost entirely toward the gating and parsing
logic, because that is where every bug in this repo has been so far — eleven
of them, each found by reading output rather than by a test. Cases carrying a
`# bug:` comment are regressions that actually shipped: `Indiana, USA` reading
as India, `Engineer, Vue 3` as level 3, `SDE III` slipping a tail-anchored
regex, `intern` matching `Internal Tools`, a posting appearing twice when it
scored identically on both resumes.

The gates deserve this weight because **a regression in them does not raise**.
It silently hides real jobs or floods the digest, and either is invisible until
someone reads the output carefully.

The suite earned itself on its first run by catching a live bug: *Member of
Technical Staff* was being rejected as senior, since `staff` is a block term.
It is not a level — it is what OpenAI, Anthropic, Mistral and Cockroach Labs
call engineers at every band — and it was silently removing 126 postings.

Both workflows run it: `ci.yml` on every push, and `digest.yml` before the poll,
so a broken gate fails the run rather than quietly mailing a bad digest.

## What is committed, and what is not

The database and the full corpus dump are **derived** — `autowork poll` rebuilds
both from scratch in about fifteen seconds, and a full dump is ~85MB. Only
state that cannot be regenerated is committed:

| File | Size | Why it must persist |
|---|---|---|
| `data/seen.txt` | ~370KB | Dedup ledger. Without it every posting looks new and the digest repeats yesterday. Sorted, so daily diffs are appended lines only. |
| `data/status.json` | small | applied / skipped / shortlisted, set from the console. |
| `data/digest/*.jsonl` | ~30KB/day | One day's shortlist, descriptions clipped to 1200 chars. |
| `data/companies.txt` | 2KB | The seed watchlist. |

This is also what carries results from the GitHub Actions run to the local
console: diffable text files, no binary merge conflicts.

## Ranking

Two stages, in `autowork/rank.py`. **Gates** are hard constraints; a posting
failing one never reaches the digest regardless of score. **Signals** are
additive and set the order among survivors. Both profiles are scored for every
posting, and the shortlist takes the better of the two.

Gates: engineering title, seniority (blocks senior/staff/lead, and levels ≥ II
including `SDE III` and trailing roman numerals), India or unscoped-remote,
stated experience bar ≤ 2 years, posted within 45 days.

Two of these are written as **allowlists on purpose**. Earlier versions
blacklisted foreign place names and non-engineering job titles, and both
leaked badly — Sweden, Munich and Buenos Aires all passed the location gate,
and "Office Operations Associate" scored 41 on incidental keyword overlap. An
enumeration of everything you *don't* want never converges; requiring positive
evidence of what you *do* want does.

Location is **tiered rather than gated**: Bangalore +12, remote +8, elsewhere
in India +2. Remote outranks a non-Bangalore Indian city because both are
acceptable but only one needs a move. Hard-gating Bangalore would halve the
eligible pool, which works against the goal — set
`constraints.require_bangalore` to `true` if that tradeoff ever changes.

The bias throughout is recall over precision: the goal is interview volume, so
ambiguous cases are admitted with a note rather than dropped. `required_years`
takes the *lowest* figure a description states for the same reason.

## Measured on the seed list

102 of 192 companies resolved to live boards in 29 seconds; polling all 102
returned 13,217 postings in 14 seconds with no failures. 99% carried a usable
description, 100% a posting date, 17% a parseable salary.

Ranking those 13,217 leaves **22 eligible**, rejecting 8,615 on role, 2,905 on
seniority, 1,613 on location, 39 on experience and 23 on age.

22 is too few. The gates are behaving correctly — the input pool is the
constraint, and 102 companies is simply not enough to sustain a daily digest.
Fixing that is the next task, in rough order of leverage: grow the watchlist
toward 400–800 boards, add Workable / SmartRecruiters / Recruitee adapters
(widely used by Indian companies, currently unsupported), and add the
aggregator sources, which have the side effect of making the ATS-only signal
meaningful.
