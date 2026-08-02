---
name: plan-review
description: Multi-perspective review of an implementation plan before any code is written — independent panelist critiques (hidden complexity, rival design, scope, integration; optionally a cross-vendor codex seat), author synthesis, and a dissent log for the human. Invoke when the user asks for a plan or design review, optionally with a profile (critique | tournament).
---

# Plan review

You are the author-orchestrator: you hold a draft plan for work not yet
implemented. A plan cannot be adversarially verified — there is no code to
execute against — so this skill optimizes for what a plan review *can* have:
decorrelated perspectives and visible dissent. Panelists critique the plan
independently, you synthesize, and every unresolved disagreement reaches the
user verbatim. For plans, the dissent log is often more valuable than the
consensus; never omit rejected critiques from it.

Launching a review is the user's cost decision: run it only when asked, or
when the user has made it standing policy.

Paths below are relative to the devenv_utils directory this skill lives under
(`subtrees/devenv_utils/` in a consumer repo; the repo root in devenv_utils'
own working clone).

## Input

The plan must be a self-contained written artifact — a file, a PR/issue
body — not "what we discussed". Panelists see only the plan text, the repo,
and their lens; that independence is the point. If the plan so far exists
only in conversation, write it to a file first.

## The codex seat

A panelist is any command that takes a prompt and returns a critique — which
is what makes a cross-vendor seat possible. Same-vendor panelists share
training-induced blind spots and architectural tastes even under disjoint
lens prompts; a different vendor's model decorrelates them, and it also
checks the structural bias of a Claude orchestrator adjudicating critiques
of a Claude-authored plan. When the `codex` CLI is on PATH and authenticated
(host-side `codex login` with `cli_auth_credentials_store = "file"` in
`~/.codex/config.toml` — the keyring default is invisible to containers —
and the dir mounted in), run the rival-designer seat as:

    codex exec --sandbox read-only "<the panelist prompt>"

capturing stdout (codex prints the final message there, progress to stderr).
When codex is absent, that seat falls back to a session-tier subagent — the
panel degrades gracefully to all-Claude.

## Critique profile (default)

Spawn all four panelists in parallel; none sees the others or your reasoning
behind the plan.

| Panelist          | Lens                              | Seat                    |
|-------------------|-----------------------------------|-------------------------|
| hidden-complexity | `panelists/hidden-complexity.md`  | subagent, session tier  |
| rival-designer    | `panelists/rival-designer.md`     | codex if available, else subagent, session tier |
| scope             | `panelists/scope.md`              | subagent, sonnet        |
| integration       | `panelists/integration.md`        | subagent, sonnet        |

Prompt template (fill the bracketed parts; lens paths made absolute):

> You are one panelist reviewing an implementation plan before any code is
> written. Read [lens path] and adopt it as your entire role. The plan:
> [plan path]. Repo root: [path] — read any code you need; modify nothing.
> Report only your own lens; do not guess what other reviewers might say.
> Return a YAML list of critiques, each entry: title, severity (blocking |
> serious | minor), argument (the concrete case, grounded in the plan text
> or in cited code), proposal (what to change). An empty list is legitimate
> only if the plan genuinely survives your lens — and your lens file may
> demand more than an empty list.

## Synthesis

For each critique, either **revise** the plan or **reject** it with a
written rationale — the same discipline as pr-review's fix-or-rebut. There
is no second panel round: plans iterate with the human, not by agent
attrition. Deliver to the user:

1. The revised plan.
2. The dissent log: every blocking/serious critique with its resolution
   (revised how / rejected why), and anything you could not resolve marked
   **open — human call**.

## Tournament profile

For large designs where patching one draft under-explores the space. Two or
three session-tier drafters each write a complete plan from the same brief,
blind to each other, each pushed toward a different premise (for example:
minimal-first, performance-first, evolvability-first). If the codex seat is
available, it takes one drafter slot. Then a fresh session-tier judge —
not you, since one draft may descend from your own thinking — compares the
drafts against the brief, picks a winner, grafts the best ideas from the
losers, and records what each losing draft got right. Deliver the synthesis
and that record to the user; optionally run the critique profile on the
synthesized plan before implementation.
