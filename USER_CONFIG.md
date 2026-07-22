# Local configuration overrides

A consumer repo's settings live in a tracked `devenv.toml` at the repo root
(see [CONSUMER_SETUP.md](CONSUMER_SETUP.md) for its fields). Alongside it, an
optional **`devenv.local.toml`** holds per-checkout overrides that stay on your
machine.

## `devenv.local.toml`

`devenv.local.toml` is never committed: the setup wizard's git step adds it to
the repo's `.git/info/exclude`, so it is ignored without editing a tracked
`.gitignore`.

It uses the same schema as `devenv.toml`. When it exists, `load_config` reads
`devenv.toml` first and then overlays `devenv.local.toml`'s top-level keys
**wholesale** -- a table such as `[submodules]` replaces the tracked one
entirely, rather than merging key by key. So a project ships defaults in
`devenv.toml`, and a developer overrides them locally without touching the
tracked file.

## `[submodules] pull_update`

`pull_update` controls what a `git pull` on `main` does when it brings in a
merge that leaves a submodule's Gitea `main` ahead of the recorded pointer:

```toml
[submodules]
pull_update = "prompt"
```

* `"prompt"` (the default when unset) -- list the new submodule commits and ask
  over the terminal whether to commit the pointer bump. Answering no offers to
  remember the choice by writing `pull_update = "never"` into
  `devenv.local.toml`.
* `"never"` -- print a one-line note that a bump is available and do nothing
  else.
* `"always"` -- commit the pointer bump without asking.

### Non-interactive pulls

The prompt reads the controlling terminal (`/dev/tty`). A pull with no terminal
-- a script, CI, or an agent-driven pull -- never blocks: it prints the same
one-line note `"never"` would and moves on.

### When the check runs

The check is a post-merge hook, so it fires only when a pull or merge on `main`
**actually merges** something. It does not run for an "Already up to date"
pull, a bare `git fetch` or remote-tracking update, or a rebase (`git pull
--rebase` and `git rebase` do not fire the post-merge hook).

To run the check on demand, regardless of what a pull did:

```
submodules/devenv_utils/submodule_guard.py offer-update
```
