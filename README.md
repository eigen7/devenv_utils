# devenv_utils

Reusable machinery for a Docker-based, AI-agent-compatible development
workflow. Each project pulls it in as a git submodule.

This README is for **humans** — how you review and land the changes an agent
produces. The agent-facing instructions live in [WORKFLOW.md](WORKFLOW.md);
setting up a new project is [CONSUMER_SETUP.md](CONSUMER_SETUP.md).

## Why this machinery exists

### Docker

You want your project development environment to work the same everywhere:
on your laptop, on your friend's desktop, and on a cloud server. Docker is a
good way to ensure that.

But Docker has some stress points, such as producing the right "docker run"
command (mount-points, exposing dev servers to the host browser via hostname
routing through the gateway, propagating the host machine's IDE/Claude
settings, file-permissions, and more).

`devenv_utils` provides tooling to set all this up for you.

### Coding agents: worktrees, pull requests, submodules

Coding agents (Claude Code and friends) work best on an isolated checkout: a
**git worktree** per task lets an agent build, test, and commit a change without
disturbing your working tree or other in-flight work. But you still want to
**review and approve** those changes the way you'd review a colleague's — a
GitHub-style pull request in the browser, with a diff and inline comments. So
this ships a **local Gitea** instance — a machine-wide Docker service container
that is simply always running (see [GITEA.md](GITEA.md)) — that gives every
worktree branch a PR page — entirely on your machine, no external service.

It also smooths over a genuinely thorny corner: **git worktrees and git
submodules interact badly.** Worktree metadata bakes in absolute paths that only
resolve inside the container; a fresh worktree doesn't populate submodules; and
publishing a change that spans a submodule must push the submodule commit to its
upstream *before* the superproject that points at it. The tooling handles all of
that, so day to day you don't think about it.

## The development workflow, from your side

The first time, you run `./setup_wizard.py`. This walks you through one-time
setup and builds the Docker image.

After that, you start a development session by running `./run_docker.py`. This
launches a Docker container and lands you inside of it, like an ssh session
into a virtual machine.

You launch your IDE, and connect to that Docker container. You can then interact
with your AI agent through your IDE, or through a CLI interface launched from
the container. Your agent sessions and IDE state are preserved across container
restarts: they live on directories mounted from the host.

You interact with the agent much as you would a colleague; the machinery stays
mostly invisible. A typical change:

1. **Ask the agent** to implement something (say, a feature in the parent repo).
2. The agent works in a worktree and hands you a **PR URL** for browser review.
   Review it like a human PR — inline comments on the Gitea page, plus direct
   conversation with the agent — over as many rounds as you need.
3. When it looks good, **merge it**: click *Merge* on the Gitea page, run the
   merge command the agent printed next to the URL, or just ask the agent to
   merge.
4. On the host, run **`git publish`** to publish the merge to GitHub. It
   fast-forwards your local checkout to the merged state, pushes to GitHub (and,
   for a change that spans a submodule, pushes the submodule commit first, in the
   order upstream requires), and removes the merged worktree.

Why a dedicated `git publish` rather than `git push`? Merging on Gitea only
advances Gitea's copy — the merge commit isn't in your local checkout yet, so a
plain `git push` has nothing to send and would silently do nothing. And for a
submodule-spanning change, only `git publish` gets the push ordering right. So
you don't get caught by the silent no-op, a **pre-push hook redirects a stray
`git push` to `git publish`** whenever a merge is waiting to be published — and
blocks the push outright when Gitea is unreachable, since publishing around
Gitea is how histories diverge (`git push --no-verify` bypasses deliberately).
(The hooks are installed by `./setup_wizard.py`, not automatically on clone.)

## Committing directly on main

You don't have to route every change through a PR — a quick tweak committed
straight to `main` is fully supported. Hooks installed by the wizard keep the
two `main`s in lockstep so the PR flow and direct commits can't drift apart
(see commit_guard.py):

- Each commit or merge on `main` is **automatically mirrored to Gitea**
  (`git push gitea main`, printed as `mirrored main -> gitea`). This touches
  only the local review service, never GitHub.
- A commit on `main` is **refused while Gitea holds merges you haven't
  published yet** — your change is still safely uncommitted at that point;
  run `git publish`, then commit. `git commit --no-verify` bypasses.
- `git publish` handles the leftover case itself: if a mirror push didn't
  land (say, the service was down), publish syncs Gitea before publishing.

One consequence: avoid `git commit --amend` / history rewrites on `main` —
the tip is already mirrored, so the next mirror push will refuse and tell you
how to reconcile. Feature branches and worktrees are untouched by all of
this.

## Submodules day to day

`submodules/devenv_utils` is a git submodule: a nested checkout of its own
repo, pinned by the parent repo to one exact commit (see
[SUBMODULES.md](SUBMODULES.md) for the full model). The wizard's git config
and hooks absorb most of the usual submodule friction; here is what the
remaining symptoms mean and what to do about them:

- **A fresh clone has an empty `submodules/devenv_utils/`.** Run any entry
  point (`./setup_wizard.py`, `./run_docker.py`) — each populates it before
  doing anything else. (`git clone --recurse-submodules` avoids the empty
  state entirely.)
- **`git status` shows `modified: submodules/devenv_utils (new commits)`.**
  The nested checkout sits at a different commit than the parent records;
  status lists the commits in between. After `pull`, `checkout`, `rebase`,
  or a merge, hooks re-sync it automatically (printing
  `synced submodules/devenv_utils -> <sha>`), so seeing this usually means
  the checkout has real local work — or hooks aren't installed (re-run
  `./setup_wizard.py`). To sync by hand: `git submodule update --init`.
- **A commit is refused with "would move the submodule ... backward".**
  That's the guard against the classic accident: a stale nested checkout
  swept up by a broad `git add`, which would silently pin the parent to an
  *older* devenv_utils. Run the two commands the message prints; use
  `git commit --no-verify` only if the rewind is truly intended.
- **`git stash` ignores edits under `submodules/devenv_utils/`.** Stash
  works per-repo; stash inside the nested repo instead:
  `git -C submodules/devenv_utils stash`.
- **Editing files under `submodules/devenv_utils/`** means committing twice
  — once inside the submodule, once for the parent's pointer bump. The
  agent's PR flow does this for you; the rules are in
  [SUBMODULES.md](SUBMODULES.md).

## Docs

- **[CONSUMER_SETUP.md](CONSUMER_SETUP.md)** — set up a new project to use this
  (scaffolding, `devenv.toml`, wiring `setup_git_config()`).
- **[WORKFLOW.md](WORKFLOW.md)** — the worktree → PR → publish workflow in full;
  written for the coding agent, and pointed at by each consumer's `CLAUDE.md`.
- **[GITEA.md](GITEA.md)** — how the machine-wide Gitea service container works
  (design, URLs, auth, lifecycle, migration).
- **[SUBMODULES.md](SUBMODULES.md)** — rules for changing this submodule.
