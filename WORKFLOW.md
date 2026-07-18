# Worktree → PR → publish workflow

> **Audience: the coding agent.** These are instructions for the agent driving
> changes in a consumer repo — a consumer's `CLAUDE.md` links here instead of
> restating them. Human maintainers want [README.md](README.md), which explains
> the same workflow from your side (review + `git publish`).

The canonical workflow for landing a change in a repo that uses devenv_utils.
Submodule-specific rules (pointer bumps, publishing order) live in
[SUBMODULES.md](SUBMODULES.md).

Unless told otherwise, never make changes directly in the main checkout
(`/workspace/repo`). Work in a git worktree and submit the result as a pull
request on the local Gitea service — a machine-wide Docker container the user
reviews from the host browser at http://localhost:3000/ (signed in
automatically; see GITEA.md). All of these commands run **in the container**
except `git publish`.

## Lifecycle

1. `submodules/devenv_utils/pr_flow.py worktree <branch>` — creates
   `/workspace/mount/worktrees/<project>/<branch>` on a new branch, with
   submodules populated (from the main checkout's copies), the `.env.json` setup
   stamp copied, and a Claude commit identity so the PR distinguishes Claude's
   commits from the user's. Worktrees live under the mount so in-progress work
   survives container relaunches.
2. Make the changes in the worktree, as atomic commits reviewable in isolation.
   A change that spans a submodule is committed twice: the commit inside
   `submodules/<name>/`, and the superproject pointer bump (see SUBMODULES.md).
3. Before opening the PR: the affected test suites must pass and changed files
   must be formatter-clean. Say what was run in the PR body.
4. `submodules/devenv_utils/pr_flow.py create <branch> --title ... --body-file ...`
   — pushes the branch and opens its PR, **plus a PR in each submodule the
   branch advances** (those merge first), as the `claude` Gitea user. It prints
   the review + merge handoff; relay that to the user.
5. Address review comments with follow-up commits — not squashes or
   force-pushes, which break the reviewer's "changes since last review" view.
6. Once the user approves, they merge each PR on its Gitea page in the browser
   (or, in the container, `submodules/devenv_utils/gitea_merge.py <repo> <N>`).
   A submodule-spanning change has a PR in each repo: merge the submodule's
   first, then the consumer's. Then, on the host, they run `git publish`.

## Accept vs publish

Merging on Gitea ("accept") only advances Gitea's `main`; nothing reaches the
local checkout or GitHub until **`git publish`**. `git publish` runs on the host
(the GitHub credentials live there) and is the only host step: it fast-forwards
the main checkout, publishes to GitHub (submodule-first), and removes the merged
worktree. It reads the merge from Gitea over the public web port, so a
referenced submodule commit needs only to be on Gitea, not yet on GitHub. A
pre-push hook redirects a stray bare `git push` to `git publish` and refuses
origin pushes from inside the container.

## Container vs host

Everything except `git publish` runs in the container; the container is the sole
authority for worktree plumbing. `git publish` is the one exception that touches
worktrees from the host, and it does so with rm + prune — not `git worktree
remove`, which chokes on the container-absolute paths baked into worktree
metadata. So never run `git worktree` against a worktree from the host; to
intervene by hand, do it in the container against the main checkout
(`git -C /workspace/repo ...`). Agents never push to `origin`.

## Abandoned worktrees

Abandoned worktrees (e.g. a task's chat was closed mid-flight) are never deleted
automatically — they may hold uncommitted work. pr_flow.py prints a report of
worktrees idle for 7+ days (also standalone via stale_worktrees.py); relay it
to the user, who decides. To delete one they've cleared, run
`submodules/devenv_utils/pr_flow.py abandon <branch>` — it removes the worktree
and its branch (even if unmerged) with no Gitea interaction.
