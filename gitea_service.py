#!/usr/bin/env python3
"""Manage the machine-wide Gitea service container from the host (GITEA.md).

The service is a single long-lived Docker container, `devenv-gitea`, shared
by every consumer project and every dev container on the machine. It runs
with `--restart unless-stopped`, so after first provisioning it is simply
always there; all state lives in a host directory recorded (together with
the chosen web port) in ~/.devenv/gitea.json.

This module owns the host side: the config file, building the service
image, creating/starting the container, and registering a consumer repo on
the service (canonical `gitea` remotes + push-to-create). The interactive
path is `SetupWizardTool.setup_gitea_service()`, or running this module
directly from a consumer repo on the host:

  submodules/devenv_utils/gitea_service.py

The dev-container launcher calls ensure_started() + dev_container_args() to
wire each dev container up to the service (network, env contract, read-only
credentials mount); the in-container view lives in gitea_client.py.
"""

import sys
from pathlib import Path

if __package__ in (None, ""):
    # Enable running this file directly (submodules/devenv_utils/gitea_service.py):
    # put the repo root on sys.path and adopt the package identity so the
    # relative imports below resolve.
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
    __package__ = "submodules.devenv_utils"

import json
import re
import subprocess
import time

from .config import DevenvConfig, load_config
from .console import SetupException, print_green
from .docker_ops import build_image, is_container_running
from .gitea_client import (
    BACKEND_URL_ENV,
    CONTAINER_CREDENTIALS_DIR,
    DEFAULT_HOST_WEB_PORT,
    DEVENV_NETWORK,
    SERVICE_BACKEND_PORT,
    SERVICE_CONTAINER,
    SERVICE_WEB_PORT,
    WEB_PORT_ENV,
    WEB_URL_ENV,
    GiteaAccess,
    ensure_project_remotes,
    register_repo,
)
from .state import get_env_json, in_docker_container

CONFIG_PATH = Path.home() / ".devenv" / "gitea.json"
DEFAULT_STATE_DIR = Path.home() / ".devenv" / "gitea"

IMAGE = SERVICE_CONTAINER
DOCKER_CONTEXT = Path(__file__).resolve().parent / "docker" / "gitea"

# Where the state dir is mounted inside the service container. Fixed:
# app.ini records absolute paths under it, so any state dir -- wherever it
# lives on the host -- works at this mount point without path rewriting.
STATE_MOUNT_PATH = "/workspace/mount/gitea"

STARTUP_TIMEOUT_S = 60

NOT_PROVISIONED_MESSAGE = (
    "The Gitea service has not been set up on this host. Run ./setup_wizard.py "
    "(or submodules/devenv_utils/gitea_service.py) first."
)


# ---- Host-global config (~/.devenv/gitea.json) ---------------------------


def load_service_config() -> dict | None:
    """{"state_dir": str, "web_port": int} for this host, or None before the
    first provisioning."""
    if not CONFIG_PATH.exists():
        return None
    return json.loads(CONFIG_PATH.read_text())


def save_service_config(service: dict):
    CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
    CONFIG_PATH.write_text(json.dumps(service, indent=2) + "\n")


def host_access(service: dict) -> GiteaAccess:
    """The host side's GiteaAccess: published loopback ports, credentials
    straight from the state dir."""
    web_port = service["web_port"]
    return GiteaAccess(
        web_url=f"http://127.0.0.1:{web_port}",
        backend_url=f"http://127.0.0.1:{web_port + 1}",
        host_web_port=web_port,
        credentials_dir=Path(service["state_dir"]) / "credentials",
    )


# ---- Admin identity ------------------------------------------------------


def git_user_email(repo_root: Path) -> str:
    result = subprocess.run(
        ["git", "config", "user.email"], capture_output=True, text=True, cwd=repo_root
    )
    return result.stdout.strip() or "dev@localhost"


def admin_username(repo_root: Path) -> str:
    """The Gitea admin username, derived from the git identity's email.

    The username is part of every Gitea URL (the repos live under it), so it
    is personalized rather than fixed. Gitea usernames may contain letters,
    digits, and ``.-_`` only; anything else in the email's local part is
    dropped.
    """
    local_part = git_user_email(repo_root).split("@")[0]
    sanitized = re.sub(r"[^0-9A-Za-z._-]", "", local_part)
    return sanitized or "dev"


# ---- Docker plumbing -----------------------------------------------------


def docker(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["docker", *args], capture_output=True, text=True)


def container_exists(name: str) -> bool:
    return docker("container", "inspect", name).returncode == 0


def ensure_network():
    if docker("network", "inspect", DEVENV_NETWORK).returncode != 0:
        subprocess.run(["docker", "network", "create", DEVENV_NETWORK], check=True)


def create_container(service: dict, repo_root: Path):
    """`docker run -d` the service container against the configured state dir.

    The restart policy is what makes the service feel like part of the
    machine: it comes back with the Docker daemon after a reboot. Ports are
    published loopback-only -- nginx signs every reachable client in as the
    admin, so the web port must not be visible to the LAN.
    """
    web_port = service["web_port"]
    uid = subprocess.check_output(["id", "-u"], text=True).strip()
    gid = subprocess.check_output(["id", "-g"], text=True).strip()
    cmd = [
        "docker", "run", "-d",
        "--name", SERVICE_CONTAINER,
        "--restart", "unless-stopped",
        "--network", DEVENV_NETWORK,
        "-p", f"127.0.0.1:{web_port}:{SERVICE_WEB_PORT}",
        "-p", f"127.0.0.1:{web_port + 1}:{SERVICE_BACKEND_PORT}",
        "-v", f"{service['state_dir']}:{STATE_MOUNT_PATH}",
        "-e", f"HOST_UID={uid}",
        "-e", f"HOST_GID={gid}",
        "-e", f"DEVENV_GITEA_ADMIN_USER={admin_username(repo_root)}",
        "-e", f"DEVENV_GITEA_ADMIN_EMAIL={git_user_email(repo_root)}",
        "-e", f"DEVENV_GITEA_ROOT_URL=http://localhost:{web_port}/",
        IMAGE,
    ]  # fmt: skip
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        hint = ""
        if "port is already allocated" in result.stderr:
            hint = (
                f"\nPort {web_port} is taken -- most likely by a still-running dev "
                "container whose image published the Gitea port itself. Stop that "
                "container and re-run."
            )
        raise SetupException(f"docker run {SERVICE_CONTAINER} failed:\n{result.stderr}{hint}")


def recreate_container(service: dict, repo_root: Path):
    """Replace the service container (all state is external, so this is always
    safe) -- picking up a rebuilt image, a changed port, or a changed state dir."""
    if container_exists(SERVICE_CONTAINER):
        subprocess.run(["docker", "rm", "-f", SERVICE_CONTAINER], check=True, capture_output=True)
    create_container(service, repo_root)


def wait_healthy(access: GiteaAccess):
    """Block until the service answers (and, on first boot, has provisioned
    the credential files the dev containers will mount)."""
    deadline = time.monotonic() + STARTUP_TIMEOUT_S
    while time.monotonic() < deadline:
        if access.healthy() and (access.credentials_dir / "admin_credentials.json").exists():
            return
        time.sleep(0.5)
    raise SetupException(
        f"The Gitea service did not come up within {STARTUP_TIMEOUT_S}s; "
        f"see `docker logs {SERVICE_CONTAINER}`."
    )


def ensure_started():
    """Start the service container if it exists but is stopped (after a reboot
    the restart policy normally beats us to it). Provisioning a missing
    container is the wizard's job, not a launch-time side effect."""
    if not container_exists(SERVICE_CONTAINER):
        raise SetupException(NOT_PROVISIONED_MESSAGE)
    if not is_container_running(SERVICE_CONTAINER):
        subprocess.run(["docker", "start", SERVICE_CONTAINER], check=True, capture_output=True)


# ---- Dev-container wiring ------------------------------------------------


def dev_container_args(host_network: bool) -> list[str]:
    """`docker run` args wiring a dev container up to the service: the env
    contract gitea_client.container_access() reads, the read-only credentials
    mount, and (under bridge networking) membership in the devenv network so
    the service resolves by DNS name. Under --network=host the container
    shares the host's loopback, so the host-shaped URLs already work."""
    service = load_service_config()
    if service is None:
        raise SetupException(NOT_PROVISIONED_MESSAGE)
    ensure_started()
    web_port = service["web_port"]
    creds_dir = Path(service["state_dir"]) / "credentials"
    args = [
        "-e", f"{WEB_PORT_ENV}={web_port}",
        "-v", f"{creds_dir}:{CONTAINER_CREDENTIALS_DIR}:ro",
    ]  # fmt: skip
    if host_network:
        args += [
            "-e", f"{WEB_URL_ENV}=http://localhost:{web_port}",
            "-e", f"{BACKEND_URL_ENV}=http://localhost:{web_port + 1}",
        ]  # fmt: skip
    else:
        args += [
            "--network", DEVENV_NETWORK,
            "-e", f"{WEB_URL_ENV}=http://{SERVICE_CONTAINER}:{SERVICE_WEB_PORT}",
            "-e", f"{BACKEND_URL_ENV}=http://{SERVICE_CONTAINER}:{SERVICE_BACKEND_PORT}",
        ]  # fmt: skip
    return args


# ---- Interactive provisioning (the wizard step) --------------------------


def propose_state_dir(cfg: DevenvConfig) -> Path:
    """The default state dir: the recorded one; else a legacy in-mount Gitea
    state dir when one exists (adopting it carries every repo, PR, and user
    over untouched); else ~/.devenv/gitea."""
    service = load_service_config()
    if service is not None:
        return Path(service["state_dir"])
    mount_dir = get_env_json(cfg.env_json_path).get("MOUNT_DIR")
    if mount_dir and (Path(mount_dir) / "gitea" / "app.ini").exists():
        return Path(mount_dir) / "gitea"
    return DEFAULT_STATE_DIR


def prompt_service_config(cfg: DevenvConfig) -> dict:
    existing = load_service_config() or {}
    default_port = existing.get("web_port", DEFAULT_HOST_WEB_PORT)
    default_state = propose_state_dir(cfg)

    print("The PR-review Gitea service runs as a single machine-wide Docker")
    print(f"container ({SERVICE_CONTAINER}), shared by all devenv projects.")
    ans = input(f"Gitea web port (published on 127.0.0.1) [{default_port}]: ").strip()
    web_port = int(ans) if ans else default_port
    ans = input(f"Gitea state directory [{default_state}]: ").strip()
    state_dir = Path(ans).expanduser().resolve() if ans else default_state
    return {"state_dir": str(state_dir), "web_port": web_port}


def wizard_setup(cfg: DevenvConfig):
    """The full interactive step: choose/confirm config, build the image,
    (re)create the container, and register this consumer repo."""
    service = prompt_service_config(cfg)
    Path(service["state_dir"]).mkdir(parents=True, exist_ok=True)
    save_service_config(service)
    build_image(IMAGE, DOCKER_CONTEXT, assets_dir=DOCKER_CONTEXT)
    ensure_network()
    recreate_container(service, cfg.repo_root)
    access = host_access(service)
    wait_healthy(access)
    register(cfg, service)
    owner = access.admin_creds()["username"]
    print_green(f"Gitea service ready: {access.browser_url(f'{owner}/{cfg.name}')}")
    print("Signed in automatically as the admin; no login needed.")


def register(cfg: DevenvConfig, service: dict):
    """Point this repo (and its populated submodules) at the service and make
    sure its server-side repo exists."""
    access = host_access(service)
    owner = access.admin_creds()["username"]
    ensure_project_remotes(access, cfg.repo_root, cfg.name, owner)
    register_repo(access, cfg.repo_root, cfg.name, owner)


def main(cfg: DevenvConfig):
    if in_docker_container():
        raise SystemExit(
            "gitea_service.py manages the service from the HOST -- run it there. "
            "Inside a dev container the service is already wired up (gitea_client.py)."
        )
    try:
        wizard_setup(cfg)
    except SetupException as e:
        raise SystemExit("\n".join(str(a) for a in e.args)) from e


if __name__ == "__main__":
    main(load_config(Path(__file__).resolve().parents[2]))
