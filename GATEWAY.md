# The gateway service

How devenv_utils exposes dev-container HTTP services to the host browser: a
single machine-wide reverse proxy ("the gateway") that routes
`http://<project>-<service>.localhost` hostnames to container ports, instead
of each project publishing host ports of its own.

This is both the design rationale and the operational reference. The Gitea
service container that the gateway is modeled on is documented in
[GITEA.md](GITEA.md); the per-project configuration it consumes is the
`[services]` table in `devenv.toml` (see [CONSUMER_SETUP.md](CONSUMER_SETUP.md)).

## Overview

- **Container** `devenv-gateway`, a [Traefik](https://traefik.io) instance
  from the local image `devenv-gateway` (built from
  [docker/gateway/](docker/gateway/)). Like `devenv-gitea` it runs with
  `--restart unless-stopped`: one instance per machine, always on, lifecycle
  independent of any dev container.
- **One published host port** for all projects: `127.0.0.1:<http_port>`
  (default 80, recorded in `~/.devenv/gateway.json`). No dev container
  publishes ports of its own.
- **Routing by hostname**: each service named in a project's `[services]`
  table is reachable at `http://<project>-<service>.localhost`. Names under
  `.localhost` resolve to loopback in browsers (RFC 6761) and in
  systemd-resolved — no host DNS configuration, no `/etc/hosts` entries.
- **Routes follow containers.** Traefik's docker provider watches the Docker
  daemon (socket mounted read-only) for containers carrying `traefik.*`
  labels; `run_docker.py` derives those labels from `[services]` at launch.
  Routes appear when a dev container starts and vanish when it exits —
  the gateway itself has no per-project state or configuration.

```
   host browser ──► 127.0.0.1:80 ─┐                  (published, loopback-only)
                                  ▼
              ┌────────── devenv-gateway (Traefik) ──────────┐
              │  Host(`scribblez-web.localhost`)  ──► :5173  │
              │  Host(`scribblez-dash.localhost`) ──► :5180  │
              │  Host(`myproj-app.localhost`)     ──► :3000  │
              │  routes discovered from container labels     │
              │  (docker.sock mounted read-only)             │
              └───────┬──────────────────────┬───────────────┘
                      ▼                      ▼      (docker network "devenv")
             scribblez container      myproj container
             (Vite on 0.0.0.0:5173)   (dev server on 0.0.0.0:3000)
```

## Why a gateway instead of published ports

Publishing each dev server's port on the host (the `required_ports` scheme
this replaces) has structural problems:

1. **Cross-project clashes.** Host ports are machine-global: two projects
   whose dev servers both default to 5173 cannot run containers at the same
   time, so every project had to claim a globally-unique port range and
   override its tools' stock defaults to match.
2. **Port bookkeeping.** Each project's `devenv.toml` maintained a list of
   forwarded ports that had to be kept in sync with what the tools actually
   bind, and stale entries (or missing ones) failed only at use time.
3. **Ports are not names.** `localhost:5180` says nothing about what it is;
   `scribblez-dash.localhost` does.

With the gateway, internal ports stop being host-global — every project can
keep its tools' stock defaults (Vite's 5173, etc.) because those ports exist
only inside that project's container and on the `devenv` network. The one
host resource all projects share is the gateway's single loopback port.

The per-repo-clone `INSTANCE` port-offset machinery fell out entirely: it
existed to let parallel clones of one project publish shifted copies of the
same port list, a need that pr_flow.py worktrees (many branches, one
container) superseded.

## How routing works

`devenv.toml` declares services as a named table — name to container port:

```toml
[services]
web = 5173
dash = 5180
```

At launch, `run_docker.py` attaches to the dev container, for each service,
labels of the form:

```
traefik.enable=true
traefik.http.routers.<project>-<service>.rule=Host(`<project>-<service>.localhost`)
traefik.http.routers.<project>-<service>.entrypoints=web
traefik.http.routers.<project>-<service>.service=<project>-<service>
traefik.http.services.<project>-<service>.loadbalancer.server.port=<port>
```

Traefik forwards matching requests over the `devenv` network to the
container's port. WebSockets pass through transparently (Vite HMR included),
so a dev server behind the gateway needs exactly two properties:

- **Bind beyond loopback** (`0.0.0.0`, e.g. Vite `host: true`) so the
  gateway can reach it across the docker network.
- **Accept the hostname.** Servers that validate the `Host` header must
  allow their `<project>-<service>.localhost` name. Vite's default
  `server.allowedHosts` already admits `.localhost` subdomains; other
  servers may need an explicit allowance.

Service names become DNS labels and env-var suffixes, so they must match
`[a-z][a-z0-9-]*` (and the project `name` likewise). A service that is not a
plain int uses the table form, which exposes the escape hatch for non-HTTP
traffic — hostname routing only works for HTTP/WS, so a raw-TCP service can
opt back into a loopback-published host port:

```toml
[services]
web = 5173
debugger = { port = 9229, publish = true }   # published at 127.0.0.1:9229
```

Published ports are loopback-bound and, being host-global again, are the one
place clashes remain possible — use them only where hostname routing cannot
work.

### What in-container code sees

For each service, the launcher exports
`DEVENV_SERVICE_URL_<NAME>` (name uppercased, `-` → `_`) — the URL at which
the *host browser* reaches that service, e.g.
`DEVENV_SERVICE_URL_WEB=http://scribblez-web.localhost` (a non-80 gateway
port appears as an explicit `:<port>` suffix). Tools that print or open
browser URLs read this instead of assembling `http://localhost:<port>`;
when the variable is absent (running outside the container) the localhost
form remains the right fallback. In-container clients (readiness probes,
proxies, tests) keep using `localhost:<port>` directly — the gateway is for
the host's browser, not for intra-container traffic.

`run_docker.py` prints the full service → URL table at every launch and
attach, so the entry points are always one glance away.

### Host networking

Under `--network=host` the container is not on the `devenv` network, so the
gateway cannot route to it — but its services bind the host's interfaces
directly, so nothing needs routing: the launcher skips the labels and
exports `http://localhost:<port>` URLs instead. `publish` entries are
likewise moot there.

### The gateway's own dashboard

Traefik's API dashboard is routed at `http://devenv-gateway.localhost` —
the live table of routers and services, useful to check what the gateway
currently knows when a URL misbehaves. Like everything else it is reachable
only from the host's loopback.

### What stays off the gateway

**Gitea.** The Gitea remote URL is written into every repo's `.git/config`
and must work for host-side git without a browser in the loop
([GITEA.md](GITEA.md)); rerouting it through the gateway would churn that
canonical URL scheme for no benefit. The two service containers are
siblings, not layers.

**Loopback-by-design servers.** A server that binds `127.0.0.1` inside the
container on purpose (e.g. an API holding credentials, fronted by a dev
server's proxy) is invisible to the gateway by construction. That is a
feature: putting it in `[services]` would be a no-op that times out, so
such servers simply stay out of the table.

## Component responsibilities

| Piece | Side | Role |
| --- | --- | --- |
| [docker/gateway/](docker/gateway/) | image | Traefik (pinned version) plus its static configuration: the `web` entrypoint on :80, the docker provider (label-derived routes only, `devenv` network), and the dashboard router. |
| [gateway_service.py](gateway_service.py) | host | Owns `~/.devenv/gateway.json` (the published port), builds the image, (re)creates/starts the container, and derives each dev container's labels, published ports, and `DEVENV_SERVICE_URL_*` env from its `[services]` table. Run directly from a consumer repo, or through the wizard step `SetupWizardTool.setup_gateway_service()`. |
| [cli.py](cli.py) / [docker_ops.py](docker_ops.py) | host | `run_docker.py` path: starts the gateway if stopped, attaches the labels/env/publishes to the dev container, prints the URL table. Refuses to launch a project with services if the gateway was never provisioned (the wizard owns interactive setup). |
| [config.py](config.py) | both | Parses `[services]` (int or `{port, publish}` per name) and validates names. |

## Lifecycle

**First-time setup** (wizard, host): `setup_gateway_service()` prompts for
the HTTP port (default 80), builds the image, (re)creates the container, and
waits for it to answer. Recreating is always safe — the gateway holds no
state beyond its static config, which is baked into the image.

**Every dev-container launch** (`run_docker.py`, host): `docker start
devenv-gateway` if it exists but is stopped (after a reboot the restart
policy has normally already handled it), then launch the dev container with
labels and env derived from the current `devenv.toml` — so editing
`[services]` takes effect on the next container launch, with no gateway-side
action.

**Steady state**: nothing manages the gateway at all; routes track dev
containers by themselves.

**Manual control**: `docker stop|start|logs devenv-gateway`. To change the
published port: re-run the wizard (or `gateway_service.py`). To reset
entirely: `docker rm -f devenv-gateway` and re-run the wizard — there is no
state to lose.

## Failure modes

- **Gateway not running** (stopped by hand, Docker down): every
  `*.localhost` URL gets connection-refused. `docker start devenv-gateway`
  on the host. Dev containers never attempt to launch or repair it.
- **Port 80 already bound at creation**: something else serves HTTP on the
  host. Re-run the wizard and pick another port; URLs then carry an explicit
  `:<port>` suffix.
- **URL resolves but 404s**: the gateway is up but has no such route — the
  project's dev container is not running, or the service name is not in its
  `[services]` table. Check `http://devenv-gateway.localhost` for the live
  route list.
- **URL routes but times out / connection-refused upstream**: the route
  exists but the dev server behind it is not listening on that port, or is
  bound to loopback instead of `0.0.0.0`.
- **Hostname does not resolve** (non-browser tools on unusual hosts):
  browsers and systemd-resolved resolve `*.localhost` themselves; a bare
  resolver that refuses may need `curl --resolve
  <name>.localhost:80:127.0.0.1` or an `/etc/hosts` entry. Browsers are the
  primary consumer, and they always work.
- **A raw-TCP client can't use a hostname route**: expected — hostname
  routing is HTTP/WS-only. Use `publish = true` for that service.

## Security

The published port is loopback-bound: nothing is exposed to the LAN. The
docker socket is mounted read-only into the gateway container — sufficient
for the docker provider's event stream, and the container runs only the
pinned upstream Traefik binary with a static config. Anyone who can reach
the gateway can reach every dev server on the machine, which is the same
trust boundary the old published ports drew.
