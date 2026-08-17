---
description: Tailor a resume for a shortlisted role
argument-hint: <rank number from the console or `autowork top --all`>
allowed-tools: Bash(uv run autowork:*), Read, Write
---

## The role

!`uv run autowork show $1`

## Your task

Produce a resume tailored to this posting.

**Pick the base first.** The `best_profile` field names which of their resumes
scored highest; `uv run autowork whoami` lists each profile and its file. Read
another one too if the JD straddles them, but commit to one as the base —
merging produces a resume that reads like neither. Say which you chose and why,
in one line.

**The hard rule: every claim must trace to a line in the base resume.** You may
re-order, re-weight, re-word, promote a buried detail into a headline, drop
anything irrelevant, and change emphasis freely. You may not invent a
technology, a metric, a scope, or a responsibility that is not already there.
If the JD wants something they genuinely have not done, the honest move is to
leave it out — not to imply it. A resume that wins a screen and loses the
interview is worse than one that never got the screen.

Work through it in this order:

1. **Rewrite the summary** for this specific role and company. Three lines at most.
2. **Re-order and re-word the bullets** so the ones this JD rewards come first.
   Keep every number that is already in the resume — percentages, costs saved,
   volumes, timelines. Numbers are the strongest thing on any resume, and
   dropping one to make room is almost always the wrong trade.
3. **Rewrite the skills block** so the terms this JD actually uses appear in the
   words it uses, where the resume genuinely supports them. `requirements_covered`
   tells you which are already evidenced. Do not add anything from
   `requirements_missing` unless the base resume supports it under a different
   name — and if it does, say so explicitly in your notes.
4. **Write it** to `profile/tailored/<company>-<role-slug>.md`, lowercase and
   hyphenated, then render it:
   `uv run autowork render profile/tailored/<file>.md`
   That produces the PDF you actually upload — it auto-shrinks to one page, so
   check the reported size. If it dropped below 8pt the resume is too long and
   the fix is to cut a bullet, not to ship 7pt type.
5. **Then report separately from the document**: which base you used, the three
   biggest changes you made and why, and any `requirements_missing` item you
   could not honestly address. That last list is what he prepares for in the
   interview, so do not soften it.

Keep it to one page of content. Two pages of tailored resume is worse than one
page of the right things.
