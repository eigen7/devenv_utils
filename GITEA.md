# The Gitea service

How devenv_utils provides the local Gitea instance behind the PR-review
workflow: a single host-managed Docker "service" container, shared by every
consumer project and every dev container on the machine.

This is both the design rationale and the operational reference. The
workflow that *uses* Gitea (worktrees, PRs, publish) is documented in
[WORKFLOW.md](WORKFLOW.md) and [README.md](README.md).

## Overview

Gitea gives worktree branches a GitHub-style pull-request UI — diffs, review
comments, "changes since last review" — while keeping everything on-machine.
It runs in a dedicated, long-lived Docker container:

- **Container** `devenv-gitea`, from the local image `devenv-gitea` (built
  from [docker/gitea/](docker/gitea/)). Runs with `--restart unless-stopped`,
  so it comes back with the Docker daemon after a reboot; nothing needs to
  lazily launch it.
- **One instance per machine**, shared by all consumer projects (each
  registers its own server-side repo) and by parallel dev-container
  instances. Its lifecycle is independent of any dev container's.
- **State** lives in a host directory (the *state dir*, recorded in
  `~/.devenv/gitea.json`), bind-mounted into the service container. Blowing
  away the container loses nothing.
- **Reachability**: the host reaches it on loopback-published ports
  (`127.0.0.1:<web_port>`); dev containers reach it by DNS name
  (`devenv-gitea`) over the user-defined Docker network `devenv`.

```
        host browser / host git ──► 127.0.0.1:3000 ─┐        (published, loopback-only)
                                                     ▼
                     ┌─────────────── devenv-gitea ──────────────┐
                     │  nginx :3000 ──► gitea :3001               │
                     │  (stamps admin auth)   (basic auth)        │
                     │  state: <state_dir> mounted at             │
                     │         /workspace/mount/gitea             │
                     └────────▲───────────────────▲───────────────┘
                              │ :3000             │ :3001
             dev container A ─┘    dev container B┘   (docker network "devenv",
             (git via rewrite)     (pr_flow API/push)  DNS name "devenv-gitea")
```

## Why a host-managed service container

A Gitea instance launched inside each dev container has three structural
problems:

1. **Host git commands need Gitea while no dev container is running.** The
   `gitea` remote lives in the repo's `.git/config`, which host and container
   share through the bind mount, so a plain host-side `git fetch --all` or
   `git publish` after a reboot hits a dead port until a container is
   launched.
2. **Concurrent dev containers collide.** Two projects' containers (or two
   instances of one project) would each publish a Gitea port on the host and
   each start their own `gitea` process.
3. **Shared sqlite state.** All projects share one Gitea state dir; two
   `gitea` processes over the same sqlite database is a corruption risk, not
   just a port clash.

A single service container solves all three, and does it with no new host
imposition: Docker is already the one hard requirement, the image is built
locally by the setup wizard, and `--restart unless-stopped` provides
boot-time startup without systemd units or lazy-launch hooks. (A host-native
Gitea binary was considered and rejected: it would need an nginx install and
an on-boot mechanism on the host, both of which the container gets for
free.)

## The one-URL constraint, and how URLs work

Host and container share `.git/config`, so the `gitea` remote URL must be a
single string that works from both sides. The scheme:

- **The canonical remote URL is host-shaped and credential-free:**
  `http://localhost:<web_port>/<owner>/<repo>.git`. On the host it works
  as-is against the published web port. Requests on the web port pass
  through nginx, which stamps every request with the admin identity (see
  below), so no credentials are embedded.
- **Dev containers rewrite it at the system-gitconfig level.** The container
  entrypoint writes
  `url."http://devenv-gitea:3000/".insteadOf = "http://localhost:<web_port>/"`
  into the container's `/etc/gitconfig`. Every git command inside the
  container transparently targets the service container instead. The rewrite
  cannot live in `~/.gitconfig` — that file is the host's, bind-mounted in.
  Under `--network=host` the rewrite is skipped: the container shares the
  host's loopback, so the canonical URL already resolves.
- **Tooling (non-git) URLs are passed explicitly.** The dev-container
  launcher exports `DEVENV_GITEA_WEB_PORT` (the canonical/browser port),
  `DEVENV_GITEA_WEB_URL`, and `DEVENV_GITEA_BACKEND_URL` (bases reachable
  *from inside that container*); `gitea_client.py` reads them. Python code
  never depends on git's rewrite.

Inside the service container the ports are fixed: nginx on 3000, the Gitea
backend on 3001. The host chooses only the published ports:
`127.0.0.1:<web_port>:3000` and `127.0.0.1:<web_port>+1:3001` (default
`web_port` 3000, recorded in `~/.devenv/gitea.json`).

## Auth model

Unchanged in spirit from the original design: **there is no login step,
ever.**

- **nginx (web port)** stamps every request with the
  `X-WEBAUTH-USER: <admin>` reverse-proxy header. Anyone who can reach the
  web port *is* the admin — which is why it is published loopback-only on
  the host. The browser is permanently signed in; anonymous-looking git
  fetches and pushes on the canonical remote URL act as the admin.
- **The Gitea backend (port 3001)** does plain basic auth and honors the
  reverse-proxy header only from loopback (`REVERSE_PROXY_TRUSTED_PROXIES =
  127.0.0.0/8` — i.e. only from nginx, which shares the service container's
  network namespace). Requests arriving over the `devenv` network or the
  published port carry a container/gateway source IP, so a spoofed header is
  ignored there. The backend is what `pr_flow.py` uses to act as the
  dedicated `claude` user (pushes and PR creation attributed to Claude, not
  the reviewing admin).
- **Credentials** live in `<state_dir>/credentials/`:
  `admin_credentials.json` and `claude_credentials.json`, mode 600, written
  by the service container on first provision. Both users are created at
  provision time (admin username derived from the host git identity's email,
  passed in by the launcher). Dev containers get the directory bind-mounted
  read-only at `/workspace/gitea-credentials/`; they never create users or
  write credentials.
- Dev containers can also reach nginx (port 3000 over the `devenv` network)
  and thereby act as the admin — the same trust level they have always had;
  the boundary that matters is the machine's LAN, guarded by the loopback
  bind.

## Component responsibilities

| Piece | Side | Role |
| --- | --- | --- |
| [docker/gitea/](docker/gitea/) | image | Dockerfile + entrypoint for the service container: provisions state on first boot (app.ini, sqlite migrate, admin + claude users), enforces the settings that must hold (bind address, root URL), generates nginx.conf, runs `gitea web` + nginx and exits if either dies (so the restart policy revives both). |
| [gitea_service.py](gitea_service.py) | host | Owns `~/.devenv/gitea.json` (state dir + web port), builds the image, creates/starts the container, migrates legacy state, and registers a consumer repo (sets the `gitea` remote in the repo and its submodules, seeds the server-side repo via push-to-create). Run directly from a consumer repo, or through the wizard step `SetupWizardTool.setup_gitea_service()`. |
| [gitea_client.py](gitea_client.py) | container | Access from inside a dev container: resolves the env-var URLs, loads credentials, wraps the backend API, and fails with host-side fix instructions when the service is unreachable. |
| [cli.py](cli.py) / [docker_ops.py](docker_ops.py) | host | `run_docker.py` path: starts the service container if it exists but is stopped, then launches the dev container with the `devenv` network, the `DEVENV_GITEA_*` env vars, and the read-only credentials mount. Refuses to launch if the service was never set up (the wizard owns interactive provisioning). |
| [pr_flow.py](pr_flow.py) / [gitea_merge.py](gitea_merge.py) | container | Consume `gitea_client.py`. `pr_flow.py create` also prints the stale-worktree report. |
| [publish.py](publish.py) / [prepush_guard.py](prepush_guard.py) | host | Fetch/check against the canonical remote URL directly (credential-free reads through nginx). |

## Lifecycle

**First-time setup / migration** (wizard, host): `setup_gitea_service()`
prompts for the web port and state dir — defaulting to an existing legacy
state dir (`<mount>/gitea`) when one is found, so existing history, PRs, and
users carry over untouched — then builds the image, (re)creates the
container, waits for health, and registers the consumer repo. Recreating the
container is always safe: all state is external.

The state dir is mounted at `/workspace/mount/gitea` *inside the service
container* regardless of where it lives on the host. That internal path is
load-bearing: Gitea's `app.ini` records absolute paths (`WORK_PATH`,
database, repos, logs) under it, so keeping the mount point fixed makes any
state dir — including one provisioned by an in-container Gitea of old —
work without path rewriting. The entrypoint enforces the few settings that
must differ for service-container operation (`HTTP_ADDR = 0.0.0.0` so the
backend is reachable across the Docker network, `ROOT_URL` matching the
chosen web port) idempotently on every start, and relocates legacy
credential files into `credentials/`.

**Every dev-container launch** (`run_docker.py`, host): `docker start
devenv-gitea` if it exists but is stopped (e.g. someone stopped it by hand —
after a reboot the restart policy has normally already handled it), then
launch the dev container wired up as described above.

**Steady state**: nothing manages Gitea at all. It is simply always there,
like a system service — which is what makes host-side `git fetch`, `git
publish`, and the browser work at any time, dev container or no dev
container.

**Manual control**: `docker stop|start|logs devenv-gitea`. To reset the
instance entirely: `docker rm -f devenv-gitea`, delete the state dir, re-run
the wizard (or `gitea_service.py`) — everything is re-provisioned from
scratch.

## Consumer-project integration

Per project, the footprint is small:

- `devenv.toml` needs **no Gitea entries**, and Gitea deliberately stays off
  the gateway: its remote URL is written into every repo's `.git/config` and
  must work for host-side git without a browser in the loop, so it keeps its
  own loopback-published port rather than a `.localhost` route (see
  [GATEWAY.md](GATEWAY.md)). The two service containers are siblings.
- The project's setup wizard calls `tool.setup_gitea_service()`.
- The project's Docker image does not install Gitea or nginx.

Multiple projects register distinct server-side repos under the same admin
owner; submodules shared between them (e.g. devenv_utils itself) resolve to
the same server-side repo, named after the submodule's GitHub origin.

## Failure modes

- **Service not running** (stopped by hand, Docker down): host git commands
  fail with connection-refused to `localhost:<web_port>`; in-container
  tooling fails its health probe and prints the fix (`docker start
  devenv-gitea` on the host). Nothing in a dev container attempts to launch
  or repair the service — the host owns it.
- **Port already bound at creation**: something else holds
  `127.0.0.1:<web_port>` — most commonly a still-running dev container from
  before this design, whose image published the port itself. Stop that
  container and re-run the wizard.
- **Two machines / fresh state**: a state dir is machine-local, like the
  repos it mirrors; there is no cross-machine story, by design.
