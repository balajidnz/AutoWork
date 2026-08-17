---
description: Honest fit assessment for a shortlisted role
argument-hint: <rank number from `autowork top --all`>
allowed-tools: Bash(uv run autowork:*), Read
---

## Who this is for

!`uv run autowork whoami`

## The role

!`uv run autowork show $1`

## Your task

Assess whether this person should apply, and what it would take to get an
interview. Read the resume listed above against the `best_profile` field in the
role output, and read another one too if the call is close.

Judge against the context above — their experience, city, salary floor and
stated goal — not against a generic candidate. If the goal is interview volume,
a slight reach that would teach them something is worth applying to; if it is a
targeted search, it is not.

Cover, in this order and no longer than it needs to be:

1. **Verdict** — apply, apply with a tailored resume, or skip. One line, up front.
2. **Where they are strong for this specific JD** — cite actual resume lines,
   not generic praise. Identify the load-bearing items on the resume — the ones
   carrying a number, a scale or an ownership claim — and say which of them
   this JD actually rewards.
3. **Real gaps** — the `requirements_missing` list is a keyword diff and is
   dumber than you are. Say which of those genuinely matter for this role, which
   are adjacent enough to be a non-issue, and whether any is a hard blocker.
4. **What to emphasise** — the two or three things that should lead if they
   apply.
5. **Likely interview angles** — what this team will probe, given the JD.

Be honest about a weak match. Telling someone to skip a role saves more time
than a padded case for applying, and a false positive costs them an afternoon.
