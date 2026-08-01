# Worktree → PR workflow

> **Audience: the coding agent.** These are instructions for the agent
> driving changes in a repo that uses devenv_utils — a consumer's `CLAUDE.md`
> links here instead of restating them. Human maintainers want
> [README.md](README.md), which explains the same workflow from your side.

The canonical workflow for landing a change in a repo that uses devenv_utils,
and in devenv_utils' own working clone. The vendored-subtree rules (read-only
copy, pulling updates, coordinated changes) live in [SUBTREES.md](SUBTREES.md).

Unless told otherwise, never make changes directly in the main checkout.
Work in a git worktree and submit the result as a pull request on GitHub,
which the user reviews and merges there. `pr_flow.py` lives at
`subtrees/devenv_utils/` in a consumer repo and at the root of the
devenv_utils working clone; run it from the repo the change targets.

## Lifecycle

1. `pr_flow.py worktree <branch>` — creates
   `/workspace/mount/worktrees/<project>/<branch>` on a new branch, with the
   `.env.json` setup stamp copied and a Claude commit identity so the PR
   distinguishes Claude's commits from the user's. Worktrees live under the
   mount so in-progress work survives container relaunches.
2. Make the changes in the worktree, as atomic commits reviewable in
   isolation. A change under `subtrees/` is blocked by the pre-commit guard:
   it belongs in the source repo's working clone
   (`/workspace/mount/devenv_utils`), through this same workflow, as its own
   PR — see SUBTREES.md, including how to test a consumer change against an
   unmerged devenv_utils branch.
3. Before opening the PR: the affected test suites must pass and changed
   files must be formatter-clean. Say what was run in the PR body.
4. `pr_flow.py create <branch> --title ... --body-file ...` — pushes the
   branch to origin and opens its GitHub PR (or reports the one already
   open). It prints the review + merge handoff; relay that to the user.
5. Optionally — only when the user asks, or has made it their standing
   policy — run the shared `pr-review` skill
   ([skills/pr-review/SKILL.md](skills/pr-review/SKILL.md)): a bounded
   agentic review cycle (specialized reviewer subagents, adversarial
   verification, fix-or-rebut) that ends with one summary comment on the
   PR. Launching it is the user's cost decision.
6. Address review comments with follow-up commits — not squashes or
   force-pushes, which break the reviewer's "changes since last review" view.
   A plain `git push` from the worktree updates the PR. Review comments are
   readable via the GitHub API (github_access.py) or `gh pr view --comments`
   where `gh` is installed.
7. Once the user approves, they merge the PR on GitHub. Anyone can then
   `git pull` on `main` and run `pr_flow.py cleanup`, which removes the
   worktrees and local branches of merged PRs.

## What the container may push

The container authenticates to github.com with the wizard-provisioned token
(see github_access.py) and pushes **feature branches only**: the pre-push
hook blocks any in-container update of origin's `main`, which advances by PR
merges (or by the user, from the host). Force-pushes to shared branches are
against the workflow even where the hook allows them.

## Abandoned worktrees

Abandoned worktrees (e.g. a task's chat was closed mid-flight) are never
deleted automatically — they may hold uncommitted work. pr_flow.py prints a
report of worktrees idle for 7+ days (also standalone via
stale_worktrees.py); relay it to the user, who decides. To delete one they've
cleared, run `pr_flow.py abandon <branch>` — it removes the worktree and its
branch (even if unmerged) with no GitHub interaction.
