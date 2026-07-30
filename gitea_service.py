#!/usr/bin/env python3
"""Manage the machine-wide Gitea service container from the host (GITEA.md).

The service is a single long-lived Docker container, `devenv-gitea`, shared
by every consumer project and every dev container on the machine. It runs
with `--restart unless-stopped`, so after first provisioning it is simply
always there; all state lives in a host directory recorded (together with
the chosen web port) in ~/.devenv/gitea.json.

This module owns the host side: the config file, building the service
image, creating/starting the container, and registering a consumer repo on
the service (canonical `gitea` remotes + push-to-create). Registering is
per-project and idempotent, so the wizard step
`SetupWizardTool.setup_gitea_service()` -- or this module run directly from
a consumer repo on the host -- can run as often as it likes:

  submodules/devenv_utils/gitea_service.py

The web port and state dir, by contrast, are machine-wide: every project's
`gitea` remote embeds the port, and one state dir holds every project's
repos and pull requests. They are chosen once, at first provisioning, and
changed only through a deliberate separate step that reports what the change
costs and re-registers every recorded consumer on the result:

  submodules/devenv_utils/gitea_service.py reconfigure

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

import argparse
import json
import re
import subprocess
import time

from .config import DevenvConfig, load_config
from .console import SetupException, print_green, print_red, yes_no
from .docker_ops import (
    PROVISIONING_LABEL,
    build_image,
    container_exists,
    container_label,
    docker,
    image_id,
    is_container_running,
    running_containers_with_env,
)
from .gitea_client import (
    BACKEND_URL_ENV,
    CONTAINER_CREDENTIALS_DIR,
    DEFAULT_HOST_WEB_PORT,
    DEVENV_NETWORK,
    REMOTE_NAME,
    SERVICE_BACKEND_PORT,
    SERVICE_CONTAINER,
    SERVICE_WEB_PORT,
    WEB_PORT_ENV,
    WEB_URL_ENV,
    GiteaAccess,
    ensure_project_remotes,
    register_repo,
)
from .publish import main_relationship
from .state import get_env_json, in_docker_container
from .submodule_bump import gitea_read_url

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

EXISTING_SERVICE_NOTICE = (
    "Both settings are machine-wide, so the wizard leaves them alone. To change them:\n"
    "    submodules/devenv_utils/gitea_service.py reconfigure"
)

PORT_CHANGE_NOTICE = (
    "The port is embedded in the `gitea` remote of every registered checkout (and\n"
    "of its submodules), so those remotes are rewritten at the end of this run. A\n"
    "checkout not listed above keeps pointing at the old port -- until it fails to\n"
    "reach Gitea at all: `git publish` and the commit/push guards all go through\n"
    "that remote. Run gitea_service.py in such a checkout to repair and record it."
)

STATE_DIR_CHANGE_NOTICE = (
    "The state dir holds every project's server-side repos, pull requests and\n"
    "review history, plus the admin and `claude` credentials. Pointing at a fresh\n"
    "directory starts an empty Gitea: each registered project's repo is re-created\n"
    "from its local `main`, and everything below becomes invisible. Nothing is\n"
    "deleted -- {previous} is left as it is, and pointing back at it restores the\n"
    "current state."
)

STALE_CONTAINERS_NOTICE = (
    "These running dev containers were launched against the current configuration\n"
    "and hold it in their environment and credentials mount. They keep using it\n"
    "until they are exited and relaunched:"
)

# Local `main` states that mean Gitea holds work the checkout has not published
# (see unpublished_merges); the remaining states need no warning.
UNPUBLISHED_STATES = {
    "behind": "Gitea's main has commits this checkout lacks",
    "diverged": "Gitea's main and this checkout have diverged",
}

# Cap on Gitea's paginated list endpoints, so the report of what a state dir
# holds isn't silently cut off at the API's default page size.
API_PAGE_LIMIT = 100


# ---- Host-global config (~/.devenv/gitea.json) ---------------------------


def load_service_config() -> dict | None:
    """This host's service config, or None before the first provisioning:

      state_dir  host directory holding all Gitea state
      web_port   loopback port the web front is published on
      consumers  repo roots registered on the service (see record_consumer)

    Only written once the container it describes is up, so the recorded config
    always names a configuration that worked.
    """
    if not CONFIG_PATH.exists():
        return None
    return json.loads(CONFIG_PATH.read_text())


def save_service_config(service: dict):
    CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
    CONFIG_PATH.write_text(json.dumps(service, indent=2) + "\n")


def consumer_roots(service: dict) -> list[Path]:
    """The recorded consumer checkouts that still exist."""
    return [Path(p) for p in service.get("consumers", []) if (Path(p) / "devenv.toml").is_file()]


def record_consumer(service: dict, repo_root: Path):
    """Remember a consumer checkout in the host config.

    Both machine-wide settings are baked into every registered checkout -- the
    web port into its `gitea` remotes, the state dir into the server-side repo
    those remotes address -- and a checkout the host cannot name is a checkout a
    later change cannot fix. This is what lets reconfigure() re-register every
    project on the machine instead of only the one that happens to run it.
    """
    recorded = service.setdefault("consumers", [])
    resolved = str(Path(repo_root).resolve())
    if resolved not in recorded:
        recorded.append(resolved)
        save_service_config(service)


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


def ensure_network():
    if docker("network", "inspect", DEVENV_NETWORK).returncode != 0:
        subprocess.run(["docker", "network", "create", DEVENV_NETWORK], check=True)


def provisioning_signature(service: dict) -> str:
    """What a service container was created from: the settings the user chooses,
    plus the image content. Recorded as a container label so a repeat setup run
    can tell an already-current container from one that must be replaced."""
    return json.dumps(
        {
            "state_dir": service["state_dir"],
            "web_port": service["web_port"],
            "image": image_id(IMAGE),
        },
        sort_keys=True,
    )


def container_up_to_date(service: dict) -> bool:
    label = container_label(SERVICE_CONTAINER, PROVISIONING_LABEL)
    return container_exists(SERVICE_CONTAINER) and label == provisioning_signature(service)


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
        "--label", f"{PROVISIONING_LABEL}={provisioning_signature(service)}",
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


def ensure_provisioned(service: dict, repo_root: Path):
    """Build the image and leave a healthy service container running `service`.

    A container already created from these settings and this image is kept (just
    started, if someone had stopped it), so a repeat setup run doesn't interrupt
    the other projects sharing the service. Otherwise it is replaced -- and if
    the replacement fails to come up, the last recorded configuration is
    restored: a container must be removed before one on a different port or
    state dir can take its place, so a bad choice would otherwise leave every
    project on the machine with no Gitea at all.
    """
    build_image(IMAGE, DOCKER_CONTEXT, assets_dir=DOCKER_CONTEXT)
    ensure_network()
    if container_up_to_date(service):
        ensure_started()
        wait_healthy(host_access(service))
        return
    previous = load_service_config()
    try:
        recreate_container(service, repo_root)
        wait_healthy(host_access(service))
    except (SetupException, subprocess.CalledProcessError):
        if previous is not None and previous != service:
            print_red(f"Restoring the previous configuration:\n{describe_service(previous)}")
            recreate_container(previous, repo_root)
            wait_healthy(host_access(previous))
        raise


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
    """Ask for the two machine-wide settings, defaulting to the recorded ones."""
    existing = load_service_config() or {}
    default_port = existing.get("web_port", DEFAULT_HOST_WEB_PORT)
    default_state = propose_state_dir(cfg)
    ans = input(f"Gitea web port (published on 127.0.0.1) [{default_port}]: ").strip()
    web_port = int(ans) if ans else default_port
    ans = input(f"Gitea state directory [{default_state}]: ").strip()
    state_dir = Path(ans).expanduser().resolve() if ans else default_state
    return {"state_dir": str(state_dir), "web_port": web_port}


def describe_service(service: dict) -> str:
    """The machine-wide settings and the projects registered against them.

    The project list is always shown, empty or not: a checkout missing from it
    is one that a port or state-dir change cannot repair, so its absence is the
    reader's cue to run setup there.
    """
    lines = [
        f"  web port:  {service['web_port']}",
        f"  state dir: {service['state_dir']}",
        "  registered projects:",
    ]
    roots = consumer_roots(service)
    if not roots:
        return "\n".join(lines + ["    none yet (each project records itself when its setup runs)"])
    return "\n".join(lines + [f"    {root}" for root in roots])


def wizard_setup(cfg: DevenvConfig):
    """The wizard step: provision the service if this host has none, then
    register this consumer repo on it.

    Only a host without a recorded service is asked for the port and state dir;
    on every later run they are reported and left alone, because they are shared
    by every project on the machine (reconfigure() is the way to change them).
    """
    service = load_service_config()
    if service is None:
        print("The PR-review Gitea service runs as a single machine-wide Docker")
        print(f"container ({SERVICE_CONTAINER}), shared by all devenv projects.")
        service = prompt_service_config(cfg)
    else:
        print("Using the Gitea service already provisioned on this host:")
        print(describe_service(service))
        print(EXISTING_SERVICE_NOTICE)
    Path(service["state_dir"]).mkdir(parents=True, exist_ok=True)
    ensure_provisioned(service, cfg.repo_root)
    save_service_config(service)
    register(cfg, service)
    access = host_access(service)
    owner = access.admin_creds()["username"]
    print_green(f"Gitea service ready: {access.browser_url(f'{owner}/{cfg.name}')}")
    print("Signed in automatically as the admin; no login needed.")


def register(cfg: DevenvConfig, service: dict):
    """Point this repo (and its populated submodules) at the service, make sure
    its server-side repo exists, and record the checkout in the host config."""
    access = host_access(service)
    owner = access.admin_creds()["username"]
    ensure_project_remotes(access, cfg.repo_root, cfg.name, owner)
    register_repo(access, cfg.repo_root, cfg.name, owner)
    record_consumer(service, cfg.repo_root)


def register_consumers(service: dict):
    """Re-register every recorded consumer checkout, so no project is left
    holding remotes for a port that moved or a repo the new state dir lacks.
    Checkouts that no longer exist are dropped from the config."""
    roots = consumer_roots(service)
    if [str(root) for root in roots] != service.get("consumers", []):
        service["consumers"] = [str(root) for root in roots]
        save_service_config(service)
    for root in roots:
        print(f"Registering {root}...")
        register(load_config(root), service)


# ---- Reconfiguration -----------------------------------------------------


def git(repo_root: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["git", *args], cwd=repo_root, capture_output=True, text=True)


def gitea_main_tip(repo_root: Path) -> str:
    """The `main` tip of a consumer's server-side repo, read through the
    checkout's own `gitea` remote; "" when it has no such remote (recorded but
    never registered) or the service is unreachable."""
    if git(repo_root, "config", f"remote.{REMOTE_NAME}.url").returncode != 0:
        return ""
    result = git(repo_root, "ls-remote", gitea_read_url(repo_root), "main")
    return result.stdout.split()[0] if result.returncode == 0 and result.stdout else ""


def unpublished_merges(service: dict) -> list[str]:
    """One line per recorded consumer whose Gitea `main` holds commits its local
    checkout lacks -- merges that exist only in the state dir until `git publish`
    lands them in the checkout and on GitHub."""
    lines = []
    for root in consumer_roots(service):
        tip = gitea_main_tip(root)
        relation = main_relationship(root, tip) if tip else "equal"
        if relation in UNPUBLISHED_STATES:
            lines.append(f"{root}: {UNPUBLISHED_STATES[relation]}; run `git publish` there first")
    return lines


def open_pull_requests(access: GiteaAccess) -> list[str]:
    """"<repo> #<number>  <title>" for every open PR on the service."""
    admin = access.admin_creds()
    lines = []
    for repo in access.api("GET", f"/user/repos?limit={API_PAGE_LIMIT}", admin) or []:
        path = f"/repos/{repo['full_name']}/pulls?state=open&limit={API_PAGE_LIMIT}"
        pulls = access.api("GET", path, admin) or []
        lines += [f"{repo['name']} #{pr['number']}  {pr['title']}" for pr in pulls]
    return lines


def report_outstanding_work(service: dict):
    """Report what lives only in the state dir that is about to be left behind."""
    access = host_access(service)
    if not access.healthy():
        print("  (the service is not running, so what it holds cannot be listed)")
        return
    outstanding = [f"open PR:     {line}" for line in open_pull_requests(access)]
    outstanding += [f"unpublished: {line}" for line in unpublished_merges(service)]
    if not outstanding:
        print("  (no open PRs, and every registered checkout is level with Gitea)")
    for line in outstanding:
        print(f"  {line}")


def confirm_change(previous: dict, service: dict) -> bool:
    """Spell out what the change costs, then ask. Every consequence here follows
    from the settings being machine-wide, so the report covers projects other
    than the one this is being run from."""
    if service["web_port"] != previous["web_port"]:
        print(f"\nWeb port {previous['web_port']} -> {service['web_port']}")
        print(PORT_CHANGE_NOTICE)
    if service["state_dir"] != previous["state_dir"]:
        print(f"\nState dir {previous['state_dir']} -> {service['state_dir']}")
        print(STATE_DIR_CHANGE_NOTICE.format(previous=previous["state_dir"]))
        report_outstanding_work(previous)
    stale = running_containers_with_env(WEB_PORT_ENV)
    if stale:
        print(f"\n{STALE_CONTAINERS_NOTICE}")
        for name in stale:
            print(f"  {name}")
    print()
    return yes_no("Apply this change?", default_yes=False)


def reconfigure(cfg: DevenvConfig):
    """Change the machine-wide settings: report what the change affects, apply
    it, and re-register every recorded consumer on the result."""
    previous = load_service_config()
    if previous is None:
        raise SetupException(NOT_PROVISIONED_MESSAGE)
    print("The Gitea service is shared by every devenv project on this machine:")
    print(describe_service(previous))
    print()
    service = {**previous, **prompt_service_config(cfg)}
    if service == previous:
        print_green("Configuration unchanged; nothing to do.")
        return
    if not confirm_change(previous, service):
        raise SetupException("Nothing was changed.")
    Path(service["state_dir"]).mkdir(parents=True, exist_ok=True)
    ensure_provisioned(service, cfg.repo_root)
    save_service_config(service)
    record_consumer(service, cfg.repo_root)
    register_consumers(service)
    access = host_access(service)
    print_green(f"Gitea service reconfigured: {access.browser_url('')}")
    if running_containers_with_env(WEB_PORT_ENV):
        print("Exit and relaunch the dev containers listed above (./run_docker.py).")


def main(cfg: DevenvConfig):
    if in_docker_container():
        raise SystemExit(
            "gitea_service.py manages the service from the HOST -- run it there. "
            "Inside a dev container the service is already wired up (gitea_client.py)."
        )
    parser = argparse.ArgumentParser(
        description="Manage the machine-wide Gitea service backing PR review (see GITEA.md). "
        "Its web port and state dir are shared by every project on this host, so they are "
        "chosen once, at first provisioning; registering a project on the service is "
        "separate, and safe to repeat."
    )
    parser.add_argument(
        "command",
        nargs="?",
        default="setup",
        choices=["setup", "reconfigure"],
        help="setup (default): provision the service if this host has none, then "
        "register this repo on it. reconfigure: change the machine-wide web port or "
        "state dir -- reporting what the change invalidates (other projects' remotes, "
        "open PRs, unpublished merges, running dev containers) before applying it, and "
        "re-registering every recorded project afterwards.",
    )
    args = parser.parse_args()
    try:
        if args.command == "reconfigure":
            reconfigure(cfg)
        else:
            wizard_setup(cfg)
    except SetupException as e:
        raise SystemExit("\n".join(str(a) for a in e.args)) from e


if __name__ == "__main__":
    main(load_config(Path(__file__).resolve().parents[2]))
