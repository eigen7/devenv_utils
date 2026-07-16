# devenv_utils

Reusable machinery for a Docker-based development environment, shared across
several projects as a git submodule: container setup/build/run, a local Gitea
instance for pull-request review, and a git-worktree-per-task PR workflow that
coding agents drive.

On the Docker side, every consumer gets the same three thin entry points —
`setup_wizard.py` (interactive first-time setup), `build_docker_image.py`, and
`run_docker.py` — scaffolded once and driven by a declarative `devenv.toml`;
they build and launch a dev container with the checkout bind-mounted, ports
forwarded, and a `devuser` matching your host UID/GID. The rest of this page
is about the worktree/PR workflow that runs on top of that container.

This README is for **humans** — how you review and land the changes an agent
produces. The agent-facing instructions live in [WORKFLOW.md](WORKFLOW.md);
setting up a new project is [CONSUMER_SETUP.md](CONSUMER_SETUP.md).

## Why this machinery exists

Coding agents (Claude Code and friends) work best on an isolated checkout: a
**git worktree** per task lets an agent build, test, and commit a change without
disturbing your working tree or other in-flight work. But you still want to
**review and approve** those changes the way you'd review a colleague's — a
GitHub-style pull request in the browser, with a diff and inline comments. So
this ships a **local Gitea** instance, running in the container, that gives
every worktree branch a PR page — entirely on your machine, no external service.

It also smooths over a genuinely thorny corner: **git worktrees and git
submodules interact badly.** Worktree metadata bakes in absolute paths that only
resolve inside the container; a fresh worktree doesn't populate submodules; and
publishing a change that spans a submodule must push the submodule commit to its
upstream *before* the superproject that points at it. The tooling handles all of
that, so day to day you don't think about it.

## The workflow, from your side

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
`git push` to `git publish`** whenever a merge is waiting to be published. (The
hook is installed when you run the project's setup wizard — see
[CONSUMER_SETUP.md](CONSUMER_SETUP.md) — not automatically on clone.)

## Docs

- **[CONSUMER_SETUP.md](CONSUMER_SETUP.md)** — set up a new project to use this
  (scaffolding, `devenv.toml`, wiring `setup_git_config()`).
- **[WORKFLOW.md](WORKFLOW.md)** — the worktree → PR → publish workflow in full;
  written for the coding agent, and pointed at by each consumer's `CLAUDE.md`.
- **[SUBMODULES.md](SUBMODULES.md)** — rules for changing this submodule.
