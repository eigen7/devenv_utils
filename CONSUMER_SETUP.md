# Adding devenv_utils to a new project

`devenv_utils` is vendored into a consumer repo as a **read-only git subtree**
under `subtrees/devenv_utils/`. It provides the host-side dev-container tooling
(`DevenvConfig`, `SetupWizardTool`, Docker build/run, `DevTool`) plus the
read-only-subtree guard (`subtree_guard.py` + `hooks/pre-commit`).

The consumer keeps only thin glue; everything reusable lives here. Setting up a
new project is two commands plus filling in one config file.

## Quick start

From the new repo's root:

```bash
# 1. Vendor devenv_utils as a subtree.
git subtree add --prefix=subtrees/devenv_utils \
    https://github.com/eigen7/devenv_utils.git main --squash

# 2. Scaffold the thin consumer glue (won't overwrite existing files).
python3 subtrees/devenv_utils/scaffold_consumer.py
```

That writes:

| File | Purpose |
|------|---------|
| `subtrees/__init__.py` | namespace-package marker so `subtrees.devenv_utils` imports |
| `py/setup_check.py` | bridge to import repo-root `setup_common` from `py/` scripts |
| `py/tools/pull_git_subtrees.py` | update the subtree (`git subtree pull`) |
| `.github/workflows/subtree-readonly.yml` | CI guard (server-side) |
| `subtrees/README.md` | read-only-subtree docs |
| `setup_common.py` | **template** — fill in your project name/ports/versions |

Then:

```bash
# 3. Fill in setup_common.py (name, REQUIRED_PORTS, versions, any project consts).

# 4. Activate the local pre-commit guard. Do this once in your first-run setup
#    (e.g. inside setup_wizard.py's main, on the host):
python3 -c "import setup_common; setup_common.dev_tool().ensure_git_hooks()"
```

Most projects call `dev_tool().ensure_git_hooks()` from `setup_wizard.py` so it's
activated as part of normal setup. `core.hooksPath` lives in `.git/config`, which
is bind-mounted into the dev container, so this one call covers git on both the
host and inside the container.

## The read-only model

A `subtrees/<dir>/` is a **read-only mirror** of an upstream repo:

- **Update it**: `./py/tools/pull_git_subtrees.py` (a `git subtree pull`). Because
  the prefix is never edited locally and never pushed, the pull is always a clean
  fast-forward — no conflicts.
- **Change its contents**: edit the subtree's *own upstream repo*, push there,
  then pull it down here. There is deliberately **no push** from a consumer.
- **Enforcement**: direct edits to `subtrees/<dir>/` are rejected by
  `hooks/pre-commit` (local, via `core.hooksPath`) and by the `subtree-readonly`
  CI workflow (server-side, unbypassable). A `git subtree pull` is exempt — it's
  a merge, which both layers ignore.

## Updating devenv_utils itself

Edit `devenv_utils` in its own checkout, push to its `main`, then in each
consumer:

```bash
./py/tools/pull_git_subtrees.py
```

## What stays project-specific

Only `setup_common.py` (your `DevenvConfig` + `SUBTREES` + project constants) and
your own `setup_wizard.py` / `build.py` entrypoints and their custom steps.
Everything else above is generic and identical across consumers.
