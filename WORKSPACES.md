# Multi-repo workspaces

Status: design approved 2026-09-19. PR #19 implements the membership core:
manifest parsing, detection, namespacing and identity mounts. The `ws` tool
follows in later PRs.

A **workspace** is a repo that assembles existing repos as building blocks,
and inside which you change all of them so they work together. For example:

- `facelab` = faceswap + faceswap-training + videogen
- `character_swap` = faceswap + videogen + something new

The same component (faceswap) can be a block in any number of workspaces, can
come from any org or host, and knows nothing about the workspaces that use it.

## Three roles

| Role | Example | Knows about | Committed content |
|---|---|---|---|
| **Component repo** | faceswap, videogen | nothing outside itself | its own code, `devenv.toml`, its container; unchanged by this feature |
| **Workspace repo** | facelab, character_swap | its components (by URL) | `workspace.toml` (manifest), `workspace.lock` (known-good SHAs), `CLAUDE.md` (how the blocks fit), docs, skills, glue scripts |
| **devenv_utils** | | both | the `ws` tool, workspace detection, identity mounts; vendored as a subtree into components (as today) *and* into workspace repos |

**The rule that makes blocks reusable:** nothing about a workspace is ever
committed into a component. A workspace relationship exists only in the
workspace repo and in local, uncommitted state (`.env.json`,
`.claude/settings.local.json`).

## Topology on disk

Each workspace is one directory holding its own clones of its components:

```
~/projects/facelab/                  <- workspace repo (git: lichensongs/facelab)
  workspace.toml  workspace.lock  CLAUDE.md  ws  docs/  .claude/
  subtrees/devenv_utils/             <- vendored, provides ./ws
  faceswap/                          <- clone of Dual-Basis/faceswap  (gitignored)
  faceswap-mount/                    <- its data dir                  (gitignored)
  faceswap-training/  faceswap-training-mount/
  videogen/           videogen-mount/
  devenv_utils/                      <- editable clone (a "tool" component)
  xfer/                              <- cross-component handoff area

~/projects/character_swap/           <- another workspace repo
  workspace.toml  ...
  faceswap/                          <- a SEPARATE clone of the same repo
  faceswap-mount/
  videogen/  videogen-mount/
  newthing/  newthing-mount/
```

**Separate clones per workspace**, not a shared checkout. Each workspace needs
its components on its own branches with its own uncommitted work, and its own
running containers. A shared checkout would make facelab's branch switches
break character_swap. Disk cost is small (source only); `ws setup` can pass
`git clone --reference` to share object stores when that matters.

**Mounts are per workspace by default** (`<ws>/<component>-mount`). A mount
holding large read-mostly data (faceswap's model assets, videogen's 150 GB of
weights) can instead point at a shared directory through that clone's
`.env.json` `MOUNT_DIR`. That's a local, per-machine choice, never committed.
Sharing a mount between workspaces also shares its caches and worktrees
directory, so only do it for data.

## The manifest: `workspace.toml`

```toml
name = "facelab"                      # namespaces containers, hostnames, memory

[components.faceswap]
url = "git@github.com:Dual-Basis/faceswap.git"
branch = "main"                       # default branch to track

[components.faceswap-training]
url = "git@github.com:lichensongs/faceswap-training.git"

[components.videogen]
url = "git@github.com:lichensongs/videogen.git"

[components.devenv_utils]
url = "git@github.com:eigen7/devenv_utils.git"
container = false                     # source-only: edited here, no dev container
```

- A component's key is its directory name inside the workspace. An optional
  `path` overrides it, but must stay inside the workspace.
- A component without a `devenv.toml` is automatically source-only: a
  library, a third-party repo, a model-zoo checkout. It's cloned and visible
  to every container through the identity mount, but has no container.
- URLs can point anywhere git can reach with the host user's credentials
  (GitHub orgs, personal accounts, other hosts).

## The lock: `workspace.lock`

A committed record of the component SHAs known to work together:

```toml
[faceswap]
branch = "main"
commit = "79ce339..."
```

- `ws lock` writes it from the current checkouts. It refuses when a component
  is dirty, or when its commit isn't on any remote branch, because a
  collaborator couldn't fetch it.
- `ws sync --locked` checks out exactly those SHAs, which reproduces a
  known-good combination.
- Updating the lock is how a workspace PR says "these component versions work
  together." Git submodules pin the same way, but they bring detached heads
  and awkward cross-repo editing; the lock pins without that.

## Membership: detection by containment

A component's `load_config()` decides whether it's in a workspace, and which
one, without reading anything committed in the component. It walks up from
the checkout to the nearest `workspace.toml` and checks whether that manifest
lists a component at this path. A worktree of a component clone counts as that
component. If no manifest lists it, the component is standalone and behaves
exactly as today.

Membership is purely where the clone sits. A clone outside any workspace
directory is standalone; to bring one into a workspace, clone it there (see
[Migrating an existing setup](#migrating-an-existing-setup)).

So the same faceswap code behaves correctly as a clone inside facelab, a
clone inside character_swap, or a standalone clone.

## What changes when a component runs inside a workspace

| Aspect | Standalone (today) | Inside workspace `W` |
|---|---|---|
| Container name | `devenv.toml` `instance_name`, e.g. `faceswap_dev` | `W-<name>` (the devenv.toml `name`), e.g. `facelab-faceswap`. Two workspaces can run faceswap at once. |
| Gateway hostnames | `<name>-<service>.localhost` | `W-<name>-<service>.localhost` |
| Image tag | `<image>` | `<image-repo>:W`. Two workspaces on different component branches can have different Dockerfiles; the layer cache is shared. |
| Mounts | `/workspace/repo`, `/workspace/mount` | the same, **plus identity mounts:** the workspace dir at its host path, plus any component mount dir outside it (a local `MOUNT_DIR` choice to share data) |
| Default mount dir (wizard) | `devenv.toml` `default_mount_dir` | `W/<component>-mount` |
| Claude memory | per repo | `~/.claude/projects/W/memory`, written into each clone's `.claude/settings.local.json` by `ws setup` |

Identity mounts make every host path in the workspace valid in every
component's container. Handoffs go through `W/xfer/`, and git worktrees
created on the host resolve inside containers.

## The `ws` tool

Run from the workspace repo as `./ws <cmd>`, a thin entry point into the
vendored devenv_utils:

| Command | Does |
|---|---|
| `ws setup` | Clones missing components (at the lock, or the tracked branch). Runs each container component's setup wizard with workspace defaults (mount dir, image tag). Writes each clone's `.claude/settings.local.json` (`autoMemoryDirectory`). Idempotent: this is the collaborator's one command after `git clone`. |
| `ws status` | Per component: branch, dirty, ahead/behind, differs-from-lock, devenv_utils version (warns if too old to support workspaces), container running. |
| `ws sync [--locked]` | Fast-forwards components to their tracked branches, or to the lock. |
| `ws lock` | Writes `workspace.lock` from the current checkouts. |
| `ws branch <topic> [components...]` | Creates the same topic branch/worktree in several components at once, for one cross-component change. |
| `ws up <component>` | Launches or attaches to the component's dev container, interactively. |
| `ws exec <component> [--cwd ..] [--gpus ..] -- <cmd>` | Runs a command non-interactively in that component's container, starting a transient one if needed. The `devenv exec` of the facelab plan. |

A new workspace starts from `scaffold_workspace.py`. It creates the
skeleton: `workspace.toml` with a `name`, `CLAUDE.md` template, `.gitignore`
for component dirs, mounts and `xfer/`, `.claude/settings.json`, the `./ws`
entry point, and the devenv_utils subtree.

## The working loop inside a workspace

1. `ws branch <topic> faceswap faceswap-training` creates the topic branch in
   both components.
2. Edit both. Build and test with `ws exec faceswap -- py/build.py`, then
   `ws exec faceswap-training -- ...`.
3. Each component PR goes to **its own** origin (Dual-Basis, lichensongs, any
   other host), authored with the host user's credentials.
4. After the component PRs merge: `ws sync`, then `ws lock`, then a workspace
   PR with the lock update and any glue changes.

Ordering rule: components merge first, and the workspace lock follows.

A change that's only useful inside one workspace still belongs in the
component if it's the component's job. If it's glue (scripts connecting
components), it belongs in the workspace repo.

## Compatibility and rollout

- **Components need a devenv_utils version with workspace detection.** Until
  a component pulls it, that component runs standalone even inside a
  workspace: nothing breaks, and `ws status` flags it.
- **`devenv.toml` must not set `workspace`.** `load_config` rejects it,
  since a component must never be tied to one workspace.
- **This PR (#19):** manifest parsing, detection by containment,
  namespaced container, image and hostname names, the workspace default
  mount dir, and identity mounts.
- **Follow-up PRs:**
  1. `ws setup` / `status` / `sync` / `lock`, plus `scaffold_workspace.py`.
  2. `ws exec` / `up`.
  3. `ws branch`, and host-side `pr_flow`.

## Migrating an existing setup

Don't move existing clones into a workspace. Set the workspace up fresh, as a
collaborator would:

1. Clone (or pull) the workspace repo.
2. Clone each component into it. `ws setup` does this once it exists; until
   then, `git clone <url> <key>` per manifest entry.
3. Run each component's `setup_wizard.py`. It offers
   `<workspace>/<key>-mount` as the mount dir, and builds the
   workspace-tagged image.
4. Bring over artifacts that are expensive to recreate (datasets, weights,
   checkpoints) by hand, instead of downloading them again.
   - On the same filesystem, `mv` the specific directories: instant, with no
     extra space.
   - Alternatively, point the new clone's `MOUNT_DIR` at the existing mount
     to share it (see the per-machine note under
     [Topology](#topology-on-disk)).

The old standalone clones keep working as they are. Retire them once nothing
runs from them, after pushing any branches that exist only locally.

## Alternatives considered

- **Membership declared in the component** (the first version of this PR):
  it couples a component to one workspace. Rejected.
- **Git submodules:** the pinning is right, but the day-to-day editing UX
  isn't (detached HEADs, two-step commits). The lock file gives the pinning
  alone.
- **Google `repo` / west / vcstool:** similar manifests, but none of them
  know about containers, mounts or setup wizards. That's the part that needs
  building here, and the manifest itself is small.
- **One shared clone per component across workspaces**, with worktrees per
  workspace: it saves disk, but couples branches and makes container state
  ambiguous. Worth revisiting only if the disk cost becomes real.
