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
3. Push it to the submodule's upstream (coordinate with the repo owner if you
   lack push access).
4. Commit the pointer bump in the superproject: `git add submodules/<name>`.

Never commit a superproject pointer that references an unpushed submodule
commit: other clones could not fetch it. `push.recurseSubmodules=check`
(below) backstops this at push time.

To update a submodule to its upstream tip without local changes:

```
git -C submodules/<name> pull origin main
git add submodules/<name>
```

## Cloning and initialization

A plain `git clone` of a consumer repo leaves submodule directories empty.
Consumers scaffolded by `scaffold_consumer.py` self-heal: `setup_common.py`
runs `git submodule update --init` before importing from the submodule, and
every host-side entry point imports `setup_common` first. `git clone
--recurse-submodules` also works.

## Day-to-day sync

`SetupWizardTool.setup_git_config()` (call it from the consumer's setup
wizard) applies two settings:

* `submodule.recurse=true` — `git pull` / `git checkout` update each
  submodule working tree to match the recorded pointer.
* `push.recurseSubmodules=check` — git refuses to push a commit whose
  submodule pointer references a commit absent from the submodule's remote.

## Worktrees

* `git worktree add` does **not** populate submodules: run
  `git -C <worktree> submodule update --init` after creating one.
* `git worktree remove` refuses to remove a worktree containing a populated
  submodule; pass `--force`.
