---
name: pr-review
description: Agentic review cycle for an open pull request the agent authored — specialized reviewer subagents find issues, an adversarial skeptic filters them, the author fixes or rebuts, and the PR gets one distilled summary comment. Invoke when the user asks for an agentic review of a PR, optionally with a profile (light | standard | thorough).
---

# Agentic PR review cycle

You are the author-orchestrator: the session that authored the PR (or has its
worktree checked out). You fan out reviewer subagents, filter their findings
through an adversarial skeptic, address what survives, and post one distilled
summary for the human. The cycle is bounded and surfaces disagreement instead
of grinding it away — the human is always the final authority.

Launching the cycle is the user's cost decision: run it only when asked, or
when the user has made it standing policy for their PRs.

Paths below are relative to the devenv_utils directory this skill lives under
(`subtrees/devenv_utils/` in a consumer repo; the repo root in devenv_utils'
own working clone).

## Setup

Work from the PR's worktree, with the branch pushed. Compute the review range
`$(git merge-base origin/main HEAD)..HEAD`, and resolve the profile from the
invocation argument, defaulting to `standard`:

| Profile    | Panel                   | Skeptics per finding | Rounds |
|------------|-------------------------|----------------------|--------|
| `light`    | correctness, clean-code | none                 | 1      |
| `standard` | all six                 | 1                    | 2      |
| `thorough` | all six, session-tier   | 3, majority verdict  | 2      |

## Round 1: the panel

Spawn one subagent per reviewer, all in parallel — skipping any reviewer
whose dimension the diff plainly does not touch (a docs-only diff needs no
correctness or tests pass; a diff off every hot path needs no performance
pass; one touching no human-operated surface needs no ux pass — the summary
says which reviewers were skipped). The judgment-call reviewers run on a
cheaper model tier — a narrow rubric is what makes that work. Correctness
and performance run on the session's own tier: these are the dimensions
where model quality dominates the rubric — the bugs that survive an
authoring agent plus a passing test suite are the subtle kind, and judging
whether a mechanism truly wastes machine resources on a hot path is no
shallower. Under `thorough`, everything runs session-tier.

| Reviewer    | Rubric                     | Model   |
|-------------|----------------------------|---------|
| correctness | `reviewers/correctness.md` | session |
| performance | `reviewers/performance.md` | session |
| clean-code  | `reviewers/clean-code.md`  | sonnet  |
| prose       | `reviewers/prose.md`       | sonnet  |
| tests       | `reviewers/tests.md`       | sonnet  |
| ux          | `reviewers/ux.md`          | sonnet  |

Prompt template for each (fill the bracketed parts; rubric paths made
absolute):

> You are one reviewer on a panel reviewing a pull request. Read [rubric
> path] and adopt it as your entire role. Repo root: [path]. Review range:
> [base]..[HEAD] — run `git diff`/`git log` yourself. Read as much
> surrounding code as you need; modify nothing beyond what your rubric
> explicitly allows. Report your findings as a
> YAML list, one entry per finding, with keys: file, line, severity
> (must-fix | should-fix | nit), claim (one sentence), failure_scenario (a
> concrete scenario, when your rubric requires one; use a block scalar if it
> runs long), suggestion (the fix in one sentence). An empty list is a fully
> successful review — do not manufacture
> findings to appear useful. Nits are advisory: report at most a handful,
> and only ones you would actually raise with a colleague.

## The skeptic pass

A reviewer told to find flaws will find flaws, even if it has to invent
them; the skeptic is the filter that makes the panel's output trustworthy.
A finding demonstrated by execution — the probe or benchmark and its
observed output included in the finding — skips the pass entirely:
demonstration beats refutation. For every other must-fix/should-fix finding
from the **correctness** or **performance** reviewers — both make
falsifiable claims — spawn a skeptic subagent on the session's model tier
(never a downgraded model — refutation is where quality pays):

> A code reviewer claims the following about the repo at [path], review
> range [base]..[HEAD]: [the finding, verbatim]. Attempt to REFUTE this claim by
> reading the code, running targeted commands if needed. Default to REFUTED
> if the failure scenario does not concretely hold. Answer with a verdict —
> REFUTED, CONFIRMED, or PLAUSIBLE — and a two-sentence justification.

Discard REFUTED findings. CONFIRMED findings are the real work; PLAUSIBLE
ones are addressed or rebutted like any other but can never be escalated as
disputes. Under `thorough`, take the majority of three skeptics. Findings
from the other four reviewers are judgment calls rather than falsifiable
claims — accept or rebut them directly, no skeptic.

## Resolution

For each surviving finding, either **fix** it (follow-up commits in the
worktree, plain `git push` — the usual PR rules apply) or **rebut** it with a
concrete rationale. Rebuttal is a fully legitimate outcome; never fix code
you believe is correct just to close a thread.

## Round 2 (when the profile allows it)

Re-spawn only the reviewers whose findings you resolved, with the round-1
template plus:

> This is round 2. Your round-1 findings and the author's resolution of
> each: [list]. Examine the fix commits ([shas]) and the rebuttals. Report
> ONLY: fixes that do not actually resolve the finding, regressions the
> fixes introduced, and rebuttals you still dispute after reading their
> rationale. Accepting every resolution is a successful outcome.

Fix or rebut round-2 findings the same way. The cycle ends here regardless:
a finding still contested after round 2 is marked **disputed — human call**
and argued no further.

## Summary and handoff

Post ONE comment on the PR (`github_access.py`'s `api()`:
`POST /repos/<slug>/issues/<pr#>/comments`), titled `## Agentic review
cycle`, containing the profile and a table of every non-nit finding:
reviewer, file:line, claim, skeptic verdict where one ran, and resolution
(`fixed in <sha>` / `rejected: <reason>` / `disputed — human call`). Nits
worth relaying go in one short trailing list. Reviewer-subagent chatter
never goes on the PR — this summary is the human-facing artifact.

Then tell the user the counts — found, fixed, rejected, disputed — and that
disputed items need their call.
