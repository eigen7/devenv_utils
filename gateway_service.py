#!/usr/bin/env python3
"""Manage the machine-wide gateway service container from the host (GATEWAY.md).

The gateway is a single long-lived Traefik container, `devenv-gateway`, shared
by every consumer project and every dev container on the machine. It runs with
`--restart unless-stopped`, so after first provisioning it is simply always
there. Its only state -- the one host HTTP port every `*.localhost` dev URL
goes through -- lives in ~/.devenv/gateway.json.

This module owns the host side: the config file, building the service image,
creating/starting the container, and deriving each dev container's Traefik
labels, published ports, and `DEVENV_SERVICE_URL_*` env from its `[services]`
table. The interactive path is `SetupWizardTool.setup_gateway_service()`, or
running this module directly from a consumer repo on the host:

  submodules/devenv_utils/gateway_service.py

The dev-container launcher calls dev_container_args() to attach a container's
routes and env, and launch_urls() to print the service -> URL table.
"""

import sys
from pathlib import Path

if __package__ in (None, ""):
    # Enable running this file directly (submodules/devenv_utils/gateway_service.py):
    # put the repo root on sys.path and adopt the package identity so the
    # relative imports below resolve.
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
    __package__ = "submodules.devenv_utils"

import json
import subprocess
import time
import urllib.error
import urllib.request

from .config import DevenvConfig, Service, load_config
from .console import SetupException, print_green
from .docker_ops import build_image, is_container_running
from .gitea_client import DEVENV_NETWORK
from .gitea_service import ensure_network
from .state import in_docker_container

SERVICE_CONTAINER = IMAGE = "devenv-gateway"
DOCKER_CONTEXT = Path(__file__).resolve().parent / "docker" / "gateway"
CONFIG_PATH = Path.home() / ".devenv" / "gateway.json"

# Default host HTTP port the gateway publishes; the in-container Traefik
# entrypoint it maps to. A published port other than 80 shows up as an
# explicit :<port> suffix in the browser URLs.
DEFAULT_HTTP_PORT = 80
ENTRYPOINT_PORT = 80

STARTUP_TIMEOUT_S = 30

NOT_PROVISIONED_MESSAGE = (
    "The gateway service has not been set up on this host. Run ./setup_wizard.py "
    "(or submodules/devenv_utils/gateway_service.py) first."
)


# ---- Host-global config (~/.devenv/gateway.json) -------------------------


def load_service_config() -> dict | None:
    """{"http_port": int} for this host, or None before the first provisioning."""
    if not CONFIG_PATH.exists():
        return None
    return json.loads(CONFIG_PATH.read_text())


def save_service_config(service: dict):
    CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
    CONFIG_PATH.write_text(json.dumps(service, indent=2) + "\n")


# ---- URL / label construction (pure) -------------------------------------


def service_hostname(project: str, service: str) -> str:
    """The `.localhost` hostname the gateway routes to a service's port."""
    return f"{project}-{service}.localhost"


def service_env_var(service: str) -> str:
    """The env var carrying a service's browser URL into the dev container."""
    return "DEVENV_SERVICE_URL_" + service.upper().replace("-", "_")


def service_url(project: str, service: str, http_port: int) -> str:
    """The URL the host browser uses to reach a service through the gateway.
    A gateway port other than 80 appears as an explicit `:<port>` suffix."""
    suffix = "" if http_port == DEFAULT_HTTP_PORT else f":{http_port}"
    return f"http://{service_hostname(project, service)}{suffix}"


def _labels_to_args(labels: dict[str, str]) -> list[str]:
    args = []
    for key, value in labels.items():
        args += ["--label", f"{key}={value}"]
    return args


def _routing_labels(project: str, service: str, port: int) -> dict[str, str]:
    """The four router/service labels (without traefik.enable) that map a
    service's `.localhost` hostname to its container port."""
    router = f"{project}-{service}"
    host = service_hostname(project, service)
    return {
        f"traefik.http.routers.{router}.rule": f"Host(`{host}`)",
        f"traefik.http.routers.{router}.entrypoints": "web",
        f"traefik.http.routers.{router}.service": router,
        f"traefik.http.services.{router}.loadbalancer.server.port": str(port),
    }


def router_labels(project: str, service: str, port: int) -> list[str]:
    """The five docker labels (as `--label k=v` args) that route a service:
    traefik.enable plus the four router/service labels (GATEWAY.md)."""
    return _labels_to_args({"traefik.enable": "true", **_routing_labels(project, service, port)})


def container_args(project: str, services: dict[str, Service], http_port: int) -> list[str]:
    """Every `docker run` arg attaching a dev container to the gateway: the
    routing labels (traefik.enable once, then per service), a loopback `-p`
    for each `publish` service, and a `DEVENV_SERVICE_URL_*` env per service."""
    args = ["--label", "traefik.enable=true"]
    for name, svc in services.items():
        args += _labels_to_args(_routing_labels(project, name, svc.port))
    for svc in services.values():
        if svc.publish:
            args += ["-p", f"127.0.0.1:{svc.port}:{svc.port}"]
    for name in services:
        args += ["-e", f"{service_env_var(name)}={service_url(project, name, http_port)}"]
    return args


def service_urls(project: str, services: dict[str, Service], http_port: int) -> dict[str, str]:
    """Service name -> browser URL through the gateway."""
    return {name: service_url(project, name, http_port) for name in services}


def host_network_urls(services: dict[str, Service]) -> dict[str, str]:
    """Service name -> browser URL under host networking, where the gateway is
    bypassed and each service is reached at http://localhost:<port> directly
    (so no project hostname is involved)."""
    return {name: f"http://localhost:{svc.port}" for name, svc in services.items()}


# ---- Dev-container wiring -------------------------------------------------


def dev_container_args(config: DevenvConfig, host_network: bool) -> list[str]:
    """`docker run` args wiring a dev container's services to the gateway.

    Empty when the project declares no services. Under host networking the
    gateway is bypassed, so only the `DEVENV_SERVICE_URL_*` env vars (pointing
    at localhost) are emitted -- no labels, no published ports, no gateway
    required. Otherwise the gateway must be provisioned (and is started if
    stopped), and the full routing labels + published ports + env are returned."""
    if not config.services:
        return []
    if host_network:
        args = []
        for name, url in host_network_urls(config.services).items():
            args += ["-e", f"{service_env_var(name)}={url}"]
        return args
    service = load_service_config()
    if service is None:
        raise SetupException(NOT_PROVISIONED_MESSAGE)
    ensure_started()
    return container_args(config.name, config.services, service["http_port"])


def launch_urls(config: DevenvConfig, host_network: bool) -> dict[str, str]:
    """Service name -> browser URL for the table the launcher prints. Empty when
    the project declares no services; localhost URLs under host networking; else
    the gateway URLs (requires the gateway to have been provisioned)."""
    if not config.services:
        return {}
    if host_network:
        return host_network_urls(config.services)
    service = load_service_config()
    if service is None:
        raise SetupException(NOT_PROVISIONED_MESSAGE)
    return service_urls(config.name, config.services, service["http_port"])


# ---- Docker plumbing -----------------------------------------------------


def docker(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["docker", *args], capture_output=True, text=True)


def container_exists(name: str) -> bool:
    return docker("container", "inspect", name).returncode == 0


def create_container(service: dict):
    """`docker run -d` the gateway container.

    The restart policy makes the service come back with the Docker daemon after
    a reboot. The single published port is loopback-only -- anyone who can reach
    it can reach every dev server on the machine. The docker socket is mounted
    read-only for the docker provider's event stream, and the container's own
    labels route the Traefik dashboard at http://devenv-gateway.localhost."""
    http_port = service["http_port"]
    cmd = [
        "docker", "run", "-d",
        "--name", SERVICE_CONTAINER,
        "--restart", "unless-stopped",
        "--network", DEVENV_NETWORK,
        "-p", f"127.0.0.1:{http_port}:{ENTRYPOINT_PORT}",
        "-v", "/var/run/docker.sock:/var/run/docker.sock:ro",
        "--label", "traefik.enable=true",
        "--label", "traefik.http.routers.devenv-gateway.rule=Host(`devenv-gateway.localhost`)",
        "--label", "traefik.http.routers.devenv-gateway.entrypoints=web",
        "--label", "traefik.http.routers.devenv-gateway.service=api@internal",
        IMAGE,
    ]  # fmt: skip
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        hint = ""
        if "port is already allocated" in result.stderr:
            hint = (
                f"\nPort {http_port} is taken -- something else on the host already "
                "serves HTTP there. Re-run the wizard and pick another port."
            )
        raise SetupException(f"docker run {SERVICE_CONTAINER} failed:\n{result.stderr}{hint}")


def recreate_container(service: dict):
    """Replace the gateway container (it holds no state beyond its baked-in
    static config, so this is always safe) -- picking up a rebuilt image or a
    changed published port."""
    if container_exists(SERVICE_CONTAINER):
        subprocess.run(["docker", "rm", "-f", SERVICE_CONTAINER], check=True, capture_output=True)
    create_container(service)


def ensure_started():
    """Start the gateway container if it exists but is stopped (after a reboot
    the restart policy normally beats us to it). Provisioning a missing container
    is the wizard's job, not a launch-time side effect."""
    if not container_exists(SERVICE_CONTAINER):
        raise SetupException(NOT_PROVISIONED_MESSAGE)
    if not is_container_running(SERVICE_CONTAINER):
        subprocess.run(["docker", "start", SERVICE_CONTAINER], check=True, capture_output=True)


def _http_answers(url: str) -> bool:
    """True once any HTTP response arrives. Traefik answers 404 for unknown
    hosts, which still means it is up and serving."""
    try:
        with urllib.request.urlopen(url, timeout=2):
            return True
    except urllib.error.HTTPError:
        return True
    except (urllib.error.URLError, OSError):
        return False


def wait_healthy(service: dict):
    """Block until the gateway answers HTTP on its published port."""
    url = f"http://127.0.0.1:{service['http_port']}/"
    deadline = time.monotonic() + STARTUP_TIMEOUT_S
    while time.monotonic() < deadline:
        if _http_answers(url):
            return
        time.sleep(0.5)
    raise SetupException(
        f"The gateway service did not come up within {STARTUP_TIMEOUT_S}s; "
        f"see `docker logs {SERVICE_CONTAINER}`."
    )


# ---- Interactive provisioning (the wizard step) --------------------------


def prompt_http_port() -> int:
    existing = load_service_config() or {}
    default_port = existing.get("http_port", DEFAULT_HTTP_PORT)
    print("The gateway is a single machine-wide reverse proxy; every *.localhost")
    print("dev URL, for every project, is served through this one host HTTP port.")
    ans = input(f"Gateway HTTP port (published on 127.0.0.1) [{default_port}]: ").strip()
    return int(ans) if ans else default_port


def wizard_setup(cfg: DevenvConfig):
    """The full interactive step: choose/confirm the published port, build the
    image, (re)create the container, and wait for it to answer. The gateway holds
    no per-project state, so `cfg` only names the calling project for messages."""
    service = {"http_port": prompt_http_port()}
    save_service_config(service)
    build_image(IMAGE, DOCKER_CONTEXT, assets_dir=DOCKER_CONTEXT)
    ensure_network()
    recreate_container(service)
    wait_healthy(service)
    suffix = "" if service["http_port"] == DEFAULT_HTTP_PORT else f":{service['http_port']}"
    print_green(f"Gateway service ready. Dashboard: http://devenv-gateway.localhost{suffix}")


def main(cfg: DevenvConfig):
    if in_docker_container():
        raise SystemExit(
            "gateway_service.py manages the service from the HOST -- run it there. "
            "Inside a dev container the gateway is already wired up (run_docker.py)."
        )
    try:
        wizard_setup(cfg)
    except SetupException as e:
        raise SystemExit("\n".join(str(a) for a in e.args)) from e


if __name__ == "__main__":
    main(load_config(Path(__file__).resolve().parents[2]))
