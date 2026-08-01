# Migrating from Gitea/submodules to GitHub PRs + subtrees

The dev workflow moved: the local Gitea review service and the
`submodules/devenv_utils` git submodule are gone. Review now happens as
ordinary **GitHub pull requests** (the coding agent pushes feature branches
with a scoped bot token; `main` only advances by merges), and devenv_utils is
vendored into each repo as a **read-only git subtree** at
`subtrees/devenv_utils` — plain committed files, nothing to initialize.
`git publish` no longer exists; plain `git pull` / `git push` are back.

## 0. One-time: get a GitHub token

Ask David for a token for the `eigen7-claude` machine account (each machine
gets its own token; don't share token strings between machines). The setup
wizard below will prompt for it and store it at `~/.devenv/github_token`.

## 1. Per repo checkout (each of your clones)

```bash
git checkout main && git pull        # fast-forwards onto the converted main
rm -rf submodules/                   # the old submodule working tree survives
                                     # the pull as untracked files; delete it

# One-time removal of the old workflow's git config + remote:
git config --unset submodule.recurse
git config --unset push.recurseSubmodules
git config --unset status.submodulesummary
git config --unset diff.submodule
git config --unset alias.publish
git remote remove gitea

./setup_wizard.py                    # the setup-version bump forces this anyway:
                                     # prompts for the token, installs the new
                                     # hooks, provisions the devenv_utils working
                                     # clone under your mount, rebuilds the image
```

Then relaunch the dev container (`./run_docker.py`). In-flight feature
branches are unaffected; rebase them onto the new `main` as usual.

## 2. Per machine: tear down Gitea (once, after every repo on it is migrated)

The service container restarts itself on every boot (`--restart
unless-stopped`); removing it also removes that policy.

```bash
docker rm -f devenv-gitea
docker rmi devenv-gitea                       # optional: the local image
cat ~/.devenv/gitea.json                      # note the "state_dir" path
rm -rf <state_dir>                            # Gitea repos/PRs/credentials
rm ~/.devenv/gitea.json
```

## 3. What changed day to day

- **You**: commit and push `main` from the host like any normal repo. Review
  agent PRs on github.com; merge there; `git pull` when convenient.
- **The agent**: works in a worktree, opens the PR itself.

If you want to look at the worktree state in the IDE, you can use:

```
git checkout --detach <BRANCH_NAME>
# ... inspect in the IDE ...
git checkout main
```

You can get the `BRANCH_NAME` by running `git branch` - it should match the worktree name.
