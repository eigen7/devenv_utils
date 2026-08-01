# Vendored subtrees in consumer repos

A consumer repo takes devenv_utils — and any other repo under the same
owner's control — as a **git subtree** under `subtrees/<name>/`: the files
are committed into the consumer's own history as a plain directory. A fresh
clone has everything; worktrees need no population step; there are no
pointers to keep in sync.

The copy is **read-only**. It changes only by pulling from the source repo's
upstream, never by direct edits — the pre-commit hook installed by the wizard
(subtree_guard.py) blocks staged changes under `subtrees/`. This document is
the canonical reference for working with these subtrees; consumer repos link
here instead of restating the rules.

## Updating the vendored copy

```bash
subtrees/devenv_utils/pull_subtrees.py
```

pulls every vendored subtree up to its upstream `main`. It wraps the raw
form, needed only when pulling from somewhere other than upstream (see the
coordinated-change recipe below):

```bash
git subtree pull --prefix subtrees/devenv_utils \
    https://github.com/eigen7/devenv_utils.git main --squash
```

Always pass `--squash`: each sync lands as a single commit rather than
splicing the source repo's history into the consumer's, and squash and
non-squash pulls must not be mixed within one repo. The pull creates a merge
commit, which git commits without running pre-commit — so the read-only guard
never gets in its way.

## Changing devenv_utils

Changes are authored in the **working clone** at `<mount>/devenv_utils`
(provisioned by the wizard's `setup_devenv_clone` step; inside the container,
`/workspace/mount/devenv_utils`), never in a consumer's vendored copy. The
clone is a normal checkout of the devenv_utils repo, with the same
worktree → PR workflow as any consumer (WORKFLOW.md): `./pr_flow.py worktree`,
commit, `./pr_flow.py create`, review and merge on GitHub. Once the change is
on upstream `main`, each consumer picks it up with the `git subtree pull`
above.

## Coordinated changes

When consumer code depends on a devenv_utils change that has not merged yet,
the dependency runs one direction: the devenv_utils PR merges first, then the
consumer pulls and lands. To build and test the consumer change before that
merge, pull the subtree from the working clone's branch instead of upstream:

```bash
git subtree pull --prefix subtrees/devenv_utils \
    /workspace/mount/devenv_utils <branch> --squash
```

That squash commit carries the same tree the merged upstream `main` will, so
nothing needs redoing after the merge — the consumer PR just states that the
devenv_utils PR merges first, and later routine pulls from upstream reconcile
as ordinary subtree merges.
