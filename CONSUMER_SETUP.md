# Adding devenv_utils to a new project

`devenv_utils` is added to a consumer repo as a **git submodule** at
`submodules/devenv_utils/`. It provides the host-side dev-container tooling
(`DevenvConfig`, `SetupWizardTool`, Docker build/run, `DevTool`). The
submodule directory is a full checkout of this repo, so consumers change it
by committing here directly — see [SUBMODULES.md](SUBMODULES.md) for the
workflow.

The consumer keeps only thin glue; everything reusable lives here. Setting up
a new project is two commands plus filling in one config file.

## Quick start

From the new repo's root:

```bash
# 1. Add devenv_utils as a submodule.
git submodule add https://github.com/eigen7/devenv_utils.git \
    submodules/devenv_utils

# 2. Scaffold the thin consumer glue (won't overwrite existing files).
python3 submodules/devenv_utils/scaffold_consumer.py
```

That writes:

| File | Purpose |
|------|---------|
| `submodules/__init__.py` | package marker so `submodules.devenv_utils` imports |
| `submodules/README.md` | pointer to [SUBMODULES.md](SUBMODULES.md) |
| `py/setup_check.py` | bridge to import repo-root `setup_common` from `py/` scripts |
| `devenv.toml` | **template** — fill in your project name/ports/versions |
| `setup_common.py` | loads `devenv.toml`, plus any project-specific constants |
| `setup_wizard.py` | interactive first-time setup — extend its `SetupWizard` class with custom steps |
| `build_docker_image.py` | builds the local Docker image from `docker-setup/` |
| `run_docker.py` | launches (or attaches to) the dev container |

The PR-workflow tools need no per-project shim: run them straight from the
submodule (`submodules/devenv_utils/pr_flow.py`, `gitea_service.py`,
`stale_worktrees.py`) — each reads the project's `devenv.toml` itself. The
Gitea instance backing PR review is a single machine-wide service container,
provisioned by the wizard's `setup_gitea_service()` step — see
[GITEA.md](GITEA.md); it needs no per-project ports in `devenv.toml`.

Then:

1. Fill in `devenv.toml` (name, `required_ports`, versions). Add any project
   constants to `setup_common.py`, and keep its submodule-populating stanza
   above the imports: it is what lets a plain `git clone` work without
   `--recurse-submodules`.
2. Write `docker-setup/Dockerfile` — the image `setup_wizard.py` builds and
   `run_docker.py` runs (crib from an existing consumer).
3. Add any project-specific steps (data downloads, credential templates, ...)
   to `setup_wizard.py`'s `SetupWizard` class, then run `./setup_wizard.py`.
   The scaffolded wizard already covers the generic steps, including
   `setup_git_config()` — the submodule-sync git settings every checkout
   needs (see [SUBMODULES.md](SUBMODULES.md)). GPU projects should add the
   NVIDIA steps (`setup_cdi()`, `validate_nvidia_installation()`).
4. Give your `CLAUDE.md` a short "Git submodules" section that links to
   `submodules/devenv_utils/SUBMODULES.md`, so coding agents follow the
   submodule workflow without each repo restating it.

## Updating devenv_utils itself

The submodule is a full checkout: edit it in place in any consumer, commit
inside `submodules/devenv_utils/`, push to this repo's `main`, then commit
the pointer bump in the consumer. Other consumers pick it up with:

```bash
git -C submodules/devenv_utils pull origin main
git add submodules/devenv_utils
```

## Running multiple instances in parallel

To run dev containers from two clones of the same repo at once (e.g. two
independent agent sessions on separate working trees), set `"INSTANCE": N` in
the second clone's `.env.json` (default 0). `docker_launch` then, for instance
N:

- names the container `<instance_name>_N` (so the second launch starts a new
  container instead of exec'ing into the first), and
- shifts every `required_ports` entry up by `instance_port_stride * N` (stride
  defaults to 100, override via `DevenvConfig`), aborting with a clear error if
  that would collide with an instance 0..N-1.

The offset is exported into the container as the `DEVENV_INSTANCE_PORT_OFFSET`
environment variable (`instances.INSTANCE_PORT_OFFSET_ENV`). **In-container apps
that bind hard-coded ports must read this variable and add it to their default
ports**, so they bind the ports that are actually forwarded. This is the only
project-side code needed — `devenv.toml` and `setup_common.py` are untouched.

## What stays project-specific

Only `devenv.toml` (your config data), `setup_common.py` (any project
constants), `docker-setup/Dockerfile`, the custom steps on `setup_wizard.py`'s
`SetupWizard` class, and any in-container app reads of
`DEVENV_INSTANCE_PORT_OFFSET` (see "Running multiple instances" above).
Everything else above is generic and identical across consumers.
