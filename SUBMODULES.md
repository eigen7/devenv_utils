# Git submodules in consumer repos

A consumer repo takes devenv_utils — and any other repo under the same
owner's control — as a git submodule under `submodules/<name>/`. Each
submodule directory is a full checkout of its own repo, pinned to one commit
(the "pointer" recorded in the superproject; URLs live in the repo-root
`.gitmodules`). Because the directory *is* that repo, an edit under
`submodules/<name>/` is by construction a change to that repo: nothing can
silently diverge, and no read-only guard is needed.

This document is the canonical reference for working with these submodules,
for humans and coding agents alike. Consumer repos link here instead of
restating the rules.

## Changing a submodule

1. Edit in place under `submodules/<name>/`.
2. Commit **inside the submodule** — that commit belongs to the submodule's
   repo, not the superproject.
3. Commit the pointer bump in the superproject: `git add submodules/<name>`.
   A commit's hash is fixed by its content the moment it is created, so the
   pointer bump can be prepared (and reviewed) immediately — the submodule
   commit does not have to be upstream yet.

## Publishing

Publish in dependency order: a submodule commit must reach the submodule's
upstream **before** any superproject commit referencing it is published, or
other clones cannot fetch it. This is handled for you by **`git publish`**, run
on the host (where the GitHub credentials live) after the PRs are merged on
Gitea:

```
git publish
```

It reconciles the local checkout with Gitea's `main` (normally a plain
fast-forward; a diverged history, or commits that reached GitHub outside the
Gitea flow, prompt for the safe merge/rebase interactively), then pushes each
submodule pointer commit that GitHub is missing before pushing the
superproject -- so the ordering above holds automatically. `push.recurseSubmodules=check` (below) is the backstop, and a
pre-push hook redirects a stray bare `git push` to `git publish`. See
publish.py.

Coding agents cannot publish (credentials live on the host, and `git publish`
refuses to run in the container): an agent whose change touches a submodule
ends by asking the user to merge the PRs and run `git publish`.

To update a submodule to its upstream tip without local changes:

```
git -C submodules/<name> pull origin main
git add submodules/<name>
```

## Keeping the recorded pointer current

A submodule PR merges on its own -- the superproject's recorded pointer still
names the pre-merge commit until some superproject commit bumps it. Two
conveniences offer that bump so a separate pointer-only PR is never needed.

**At publish time.** When `git publish` runs, after it syncs each submodule
checkout to the recorded pointer and before it pushes, it compares each
submodule's Gitea `main` with the recorded pointer. When Gitea's `main` is
strictly ahead (a merged submodule PR the pointer hasn't caught up to), it lists
the spanned commits and offers to bump: answering yes checks the submodule out
at that tip and commits the pointer bump on `main`, which the same publish run
then pushes -- submodule commit first, so `push.recurseSubmodules=check` holds.
Declining continues the publish; the offer is a convenience, not a gate. A
diverged history, a dirty submodule checkout, or an unreachable Gitea skips the
offer with at most a warning.

**At pull time.** When a `git pull` on `main` merges something, a post-merge
hook makes the same comparison and reacts per the `[submodules] pull_update`
knob (below). In `"prompt"` mode it lists the spanned commits and asks over the
terminal; answering yes commits the pointer bump on `main` (the commit-mirror
hook forwards it to your Gitea `main`). Answering no offers to remember the
choice as `pull_update = "never"`. This check runs only when the pull actually
merges: an "Already up to date" pull and `git pull --rebase` do not fire the
post-merge hook, so they never prompt.

### The `[submodules] pull_update` knob

A `[submodules]` table in the consumer's `devenv.toml` sets how a pull reacts
to a submodule whose Gitea `main` is ahead of the recorded pointer:

```
[submodules]
pull_update = "prompt"
```

* `"prompt"` (the default when unset) -- list the spanned commits and ask over
  the terminal. A non-interactive pull (no controlling terminal: scripts, CI,
  agent-driven pulls) never blocks; it prints a one-line note instead.
* `"never"` -- print a one-line note that a bump is available and take no
  action.
* `"always"` -- commit the pointer bump without asking.

`devenv.toml` is tracked, so a `pull_update` written by the "remember my choice"
path is yours to commit.

## Cloning and initialization

A plain `git clone` of a consumer repo leaves submodule directories empty.
Consumers scaffolded by `scaffold_consumer.py` self-heal: `setup_common.py`
runs `git submodule update --init` before importing from the submodule, and
every host-side entry point imports `setup_common` first. `git clone
--recurse-submodules` also works.

## Day-to-day sync

`SetupWizardTool.setup_git_config()` (call it from the consumer's setup
wizard) applies these settings:

* `submodule.recurse=true` — `git pull` / `git checkout` update each
  submodule working tree to match the recorded pointer.
* `push.recurseSubmodules=check` — git refuses to push a commit whose
  submodule pointer references a commit absent from the submodule's remote.
* `status.submodulesummary=1` and `diff.submodule=log` — status and diff
  describe a submodule pointer change by the commits it spans (with a
  `(rewind)` marker on backward moves) instead of by raw SHAs.

(`push.recurseSubmodules=on-demand` would instead push the submodule
automatically during a superproject push, but it cannot push from a
detached-HEAD submodule checkout — the normal state of a checkout synced by
`git submodule update` — so the explicit `git publish` flow above is the
reliable path, with `check` as the guard.)

It also installs submodule_guard.py as git hooks, covering the two gaps the
settings leave:

* `git rebase` — fast-forward or not — does not update submodule working
  trees (`submodule.recurse` covers checkout and pull only). The
  post-checkout/post-merge `sync` action updates any stale submodule
  checkout, skipping with a warning one that has uncommitted changes or
  commits the recorded pointer lacks — it never discards work.
* A stale submodule checkout swept into the index by a broad `git add`
  records a *backward* pointer move that `push.recurseSubmodules=check`
  cannot catch (the older commit exists upstream). The pre-commit action
  blocks it and prints the resync commands; `git commit --no-verify`
  rewinds deliberately.

## Worktrees

* `git worktree add` does **not** populate submodules: run
  `git -C <worktree> submodule update --init` after creating one.
* pr_flow.py (the worktree/PR workflow tool) instead clones a new worktree's
  submodules from the main checkout's copies, which also covers pointers whose
  submodule commit has not been pushed upstream yet — upstream cannot serve
  those, and a detached pointer commit cannot be fetched by SHA without
  `uploadpack.allowAnySHA1InWant`.
* `git worktree remove` refuses to remove a worktree containing a populated
  submodule; pass `--force`. pr_flow.py's `merge` and `abandon` subcommands
  do this for you.
