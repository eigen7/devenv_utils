# Adding devenv_utils to a new project

`devenv_utils` is vendored into a consumer repo as a **git subtree** at
`subtrees/devenv_utils/`. It provides the host-side dev-container tooling
(`DevenvConfig`, `SetupWizardTool`, Docker build/run, `DevTool`) and the
worktree/PR workflow tools. The vendored copy is read-only — see
[SUBTREES.md](SUBTREES.md) for how it updates and how devenv_utils itself is
changed.

The consumer keeps only thin glue; everything reusable lives here. Setting up
a new project is two commands plus filling in one config file.

## Quick start

From the new repo's root:

```bash
# 1. Vendor devenv_utils as a subtree (--squash always; see SUBTREES.md).
git subtree add --prefix subtrees/devenv_utils \
    https://github.com/eigen7/devenv_utils.git main --squash

# 2. Scaffold the thin consumer glue (won't overwrite existing files).
python3 subtrees/devenv_utils/scaffold_consumer.py
```

That writes:

| File | Purpose |
|------|---------|
| `subtrees/__init__.py` | package marker so `subtrees.devenv_utils` imports |
| `subtrees/README.md` | pointer to [SUBTREES.md](SUBTREES.md) |
| `py/setup_check.py` | bridge to import repo-root `setup_common` from `py/` scripts |
| `devenv.toml` | **template** — fill in your project name, `[services]`, versions |
| `setup_common.py` | loads `devenv.toml`, plus any project-specific constants |
| `setup_wizard.py` | interactive first-time setup — extend its `SetupWizard` class with custom steps |
| `build_docker_image.py` | builds the local Docker image from `docker-setup/` |
| `run_docker.py` | launches (or attaches to) the dev container |

The PR-workflow tools need no per-project shim: run them straight from the
subtree (`subtrees/devenv_utils/pr_flow.py`, `stale_worktrees.py`) — each
reads the invoking repo's `devenv.toml` itself.

Then:

1. Fill in `devenv.toml` (name, `[services]`, versions). Add any project
   constants to `setup_common.py`.
2. Write `docker-setup/Dockerfile` — the image `setup_wizard.py` builds and
   `run_docker.py` runs (crib from an existing consumer). To include Claude
   Code, make this the last step of the Dockerfile — after the apt/toolchain
   layers and after any other tool installs (e.g. Codex):

   ```dockerfile
   ARG CLAUDE_CODE_VERSION=latest
   COPY install-claude.sh /tmp/
   RUN bash /tmp/install-claude.sh "$CLAUDE_CODE_VERSION"
   ```

   Every build resolves `claude_code_version` from `devenv.toml` (default
   `"latest"`; `"stable"` or an exact version like `"2.1.280"` also work) to
   a concrete release and passes it as that build arg. A plain
   `./build_docker_image.py` then picks up a new release while reusing every
   layer above it; with no new release the build is fully cached. Anything
   placed below the `ARG` is rebuilt on every Claude release — both because
   it sits after a changed layer and because every later `RUN` sees the build
   arg in its environment — so keep nothing after these three lines.
3. Add any project-specific steps (data downloads, credential templates, ...)
   to `setup_wizard.py`'s `SetupWizard` class, then run `./setup_wizard.py`.
   The scaffolded wizard already covers the generic steps: the workflow git
   hooks, the GitHub token the container pushes with (`setup_github_access`),
   the devenv_utils working clone (`setup_devenv_clone`), and the gateway.
   GPU projects should add the NVIDIA steps (`setup_cdi()`,
   `validate_nvidia_installation()`).
4. Give your `CLAUDE.md` a short workflow section that links to
   `subtrees/devenv_utils/WORKFLOW.md` and `SUBTREES.md`, so coding agents
   follow the worktree/PR and subtree rules without each repo restating them.
5. Optionally adopt the shared Claude Code skills under
   [skills/](skills/README.md) by adding a thin pointer skill per adopted
   skill to your `.claude/skills/` — see that README for the convention.

## Parallel work and exposing dev servers

Running several tasks at once does not need parallel containers: the PR
workflow's `pr_flow.py` worktrees (many branches checked out under one mount,
one dev container) cover that. A single container serves every worktree.

Exposing a project's dev servers to the host browser is the `[services]` table
plus the gateway. Each entry — `name = <container port>`, or the table form
`name = { port = <p>, publish = true }` for a raw-TCP service that also needs a
loopback host port — is routed by the machine-wide gateway at
`http://<project>-<name>.localhost`, so ports stay inside the container and
never clash across projects. `run_docker.py` prints the service → URL table at
every launch. See [GATEWAY.md](GATEWAY.md); a server behind the gateway only
needs to bind `0.0.0.0` (not loopback) and accept its `.localhost` hostname.

## What stays project-specific

Only `devenv.toml` (your config data, including `[services]`),
`setup_common.py` (any project constants), `docker-setup/Dockerfile`, and the
custom steps on `setup_wizard.py`'s `SetupWizard` class. Everything else above
is generic and identical across consumers.
