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
command (mount-points, port-forwarding, propagating the host machine's
IDE/Claude settings, file-permissions, and more).

`devenv_utils` provides tooling to set all this up for you.

### Coding agents: worktrees, pull requests, submodules

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
`git push` to `git publish`** whenever a merge is waiting to be published. (The
hook is installed by `./setup_wizard.py`, not automatically on clone.)

## Docs

- **[CONSUMER_SETUP.md](CONSUMER_SETUP.md)** — set up a new project to use this
  (scaffolding, `devenv.toml`, wiring `setup_git_config()`).
- **[WORKFLOW.md](WORKFLOW.md)** — the worktree → PR → publish workflow in full;
  written for the coding agent, and pointed at by each consumer's `CLAUDE.md`.
- **[SUBMODULES.md](SUBMODULES.md)** — rules for changing this submodule.
