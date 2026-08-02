# devenv_utils

Reusable machinery for a Docker-based, AI-agent-compatible development
workflow. Each project vendors it as a git subtree.

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

### Coding agents: worktrees and pull requests

Coding agents (Claude Code and friends) work best on an isolated checkout: a
**git worktree** per task lets an agent build, test, and commit a change
without disturbing your working tree or other in-flight work. You then review
and approve those changes the way you'd review a colleague's — as a **pull
request on GitHub**. The container carries a scoped GitHub token (provisioned
once by the setup wizard) that lets the agent push feature branches and open
PRs; a pre-push hook keeps it off `main`, which only advances by your merges.

## The development workflow, from your side

The first time, you run `./setup_wizard.py`. This walks you through one-time
setup — including the GitHub token the container will use, skippable if you
only want to build and run the project — and builds the Docker image.

After that, you start a development session by running `./run_docker.py`. This
launches a Docker container and lands you inside of it, like an ssh session
into a virtual machine. You launch your IDE and connect to that container;
agent sessions and IDE state live on mounted directories, so they survive
container restarts.

You interact with the agent much as you would a colleague; the machinery
stays mostly invisible. A typical change:

1. **Ask the agent** to implement something.
2. The agent works in a worktree and hands you a **PR URL** for review on
   GitHub. Review it like any PR — inline comments, plus direct conversation
   with the agent — over as many rounds as you need. (The agent's worktree
   branch also exists locally, so `git checkout <branch>` shows you the code
   in your own IDE.)
3. When it looks good, **merge it on GitHub**.
4. `git pull` when convenient. `pr_flow.py cleanup` (host or container)
   removes the worktrees and local branches of merged PRs.

Committing directly on `main` needs no ceremony: commit and push from the
host, as in any repo.

## The vendored subtree, day to day

`subtrees/devenv_utils` is a **read-only vendored copy** of this repo,
updated by `git subtree pull` (see [SUBTREES.md](SUBTREES.md) for the full
model). What the remaining symptoms mean:

- **A commit is refused with "staged changes under subtrees/".** That's the
  guard: edits go in the working clone at `<mount>/devenv_utils` and land
  through a PR there, after which consumers pull the update. The refusal
  message says exactly this; `git commit --no-verify` bypasses deliberately.
- **Pulling an update**: the `git subtree pull --squash` command in
  SUBTREES.md; each sync is one reviewable commit.
- Otherwise it behaves like any committed directory: clones, worktrees,
  stash, and status need nothing special.

## Docs

- **[CONSUMER_SETUP.md](CONSUMER_SETUP.md)** — set up a new project to use
  this (scaffolding, `devenv.toml`, the wizard).
- **[WORKFLOW.md](WORKFLOW.md)** — the worktree → PR workflow in full;
  written for the coding agent, and pointed at by each consumer's `CLAUDE.md`.
- **[COMMENTS.md](COMMENTS.md)** — the comment and documentation doctrine;
  written for the coding agent, and linked from each consumer's `CLAUDE.md`.
- **[SUBTREES.md](SUBTREES.md)** — the vendored-subtree model: updating the
  copy, changing devenv_utils, coordinated changes.
- **[GATEWAY.md](GATEWAY.md)** — the machine-wide reverse proxy that routes
  each project's dev-server URLs.
