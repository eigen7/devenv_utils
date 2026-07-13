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
other clones cannot fetch it. From the superproject root on the host (where
upstream credentials live):

```
python3 submodules/devenv_utils/push_upstream.py
```

pushes every submodule pointer commit that upstream is missing (plain
fast-forwards; divergence fails loudly), then prints the superproject push
command to run next. `push.recurseSubmodules=check` (below) makes git refuse
any superproject push that would break the order.

Coding agents cannot push upstream (credentials live on the host): an agent
whose change touches a submodule must end by asking the user to run the
command above, noting that it prints the follow-up superproject push.

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

(`push.recurseSubmodules=on-demand` would instead push the submodule
automatically during a superproject push, but it cannot push from a
detached-HEAD submodule checkout — the normal state of a checkout synced by
`git submodule update` — so the explicit push_upstream.py flow above is the
reliable path, with `check` as the guard.)

## Worktrees

* `git worktree add` does **not** populate submodules: run
  `git -C <worktree> submodule update --init` after creating one.
* pr_flow.py (the worktree/PR workflow tool) instead clones a new worktree's
  submodules from the main checkout's copies, which also covers pointers whose
  submodule commit has not been pushed upstream yet — upstream cannot serve
  those, and a detached pointer commit cannot be fetched by SHA without
  `uploadpack.allowAnySHA1InWant`.
* `git worktree remove` refuses to remove a worktree containing a populated
  submodule; pass `--force`.
