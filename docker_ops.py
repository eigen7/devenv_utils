"""Docker build/run helpers shared by the devenv setup/run scripts.

`docker_build` stages a temporary build context: it copies the project's own
docker context (Dockerfile + any project files) and then overlays the shared
scripts that ship with this package (entrypoint.sh, devuser-setup.sh). This
lets each project keep a minimal Dockerfile that `COPY`s the shared scripts
without those scripts having to physically live in the project tree.
"""

import json
import os
import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import Optional, Tuple

from .console import SetupException

# Shared shell scripts bundled with this package, overlaid into every build
# context so a project Dockerfile can `COPY entrypoint.sh` / `COPY
# devuser-setup.sh` even though they don't live in the project tree.
SHARED_DOCKER_ASSETS = Path(__file__).resolve().parent / "docker"


# ---- Version strings -----------------------------------------------------

Version = Tuple[int, ...]


def parse_version_str(version_str: str) -> Version:
    return tuple(int(x) for x in version_str.split("."))


def is_version_ok(version_str: str, minimum: str) -> bool:
    if not version_str:
        return False
    try:
        return parse_version_str(version_str) >= parse_version_str(minimum)
    except ValueError:
        return False


def major_version(version_str: str) -> int:
    """The major component (leading integer) of a dotted version, or 0 when it
    is empty or unparseable."""
    try:
        return int(version_str.split(".")[0])
    except (AttributeError, ValueError):
        return 0


# Minimum Docker Engine version the wizard accepts. Docker enables CDI
# (Container Device Interface) by default starting at 28.3.0; CDI is how a
# generated /etc/cdi/nvidia.yaml grants the GPU to the container. On older
# daemons CDI is off by default, so `--device nvidia.com/gpu=all` fails to
# resolve and GPU access inside the container breaks.
MIN_DOCKER_VERSION = "28.3.0"


def docker_server_version() -> str:
    """The Docker daemon's version (e.g. "28.3.3"), or "" if unavailable."""
    result = subprocess.run(
        ["docker", "version", "--format", "{{.Server.Version}}"],
        stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True,
    )
    if result.returncode != 0:
        return ""
    return result.stdout.strip()


def image_exists(image: str) -> bool:
    return subprocess.run(
        ["docker", "image", "inspect", image],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    ).returncode == 0


# ---- Inspection --------------------------------------------------------

# Label recording the inputs a service container was created from, so the
# managing script can tell a container that already matches the current
# configuration from one that must be recreated (gateway_service.py).
PROVISIONING_LABEL = "devenv.provisioning"


def docker(*args: str) -> subprocess.CompletedProcess:
    """Run a docker command, capturing both streams for the caller to inspect."""
    return subprocess.run(["docker", *args], capture_output=True, text=True)


def container_exists(name: str) -> bool:
    return docker("container", "inspect", name).returncode == 0


def image_id(image: str) -> str:
    """The image's content ID, or "" if it isn't built locally. Changes on every
    rebuild that produces different content, so it identifies what a container
    was created from."""
    result = docker("image", "inspect", "--format={{.Id}}", image)
    return result.stdout.strip() if result.returncode == 0 else ""


def container_label(name: str, label: str) -> str:
    """The value of `label` on container `name`; "" if either is absent."""
    template = f'--format={{{{index .Config.Labels "{label}"}}}}'
    result = docker("container", "inspect", template, name)
    return result.stdout.strip() if result.returncode == 0 else ""


def running_containers_with_env(var_name_prefix: str) -> list[str]:
    """Names of running containers with an environment variable whose name starts
    with `var_name_prefix`.

    The `DEVENV_*` variables a dev container is launched with are baked in at
    launch, so this identifies the containers that would keep using a service's
    old configuration after it changes.
    """
    ids = docker("ps", "--quiet").stdout.split()
    if not ids:
        return []
    listing = docker("container", "inspect", "--format={{.Name}} {{json .Config.Env}}", *ids)
    names = []
    for line in listing.stdout.splitlines():
        name, _, env_json = line.partition(" ")
        env = json.loads(env_json) or []
        if any(entry.startswith(var_name_prefix) for entry in env):
            names.append(name.lstrip("/"))
    return names


# ---- Build -------------------------------------------------------------

def _copy_into(src: Path, dst: Path):
    """Copy the *contents* of directory `src` into existing directory `dst`."""
    shutil.copytree(src, dst, dirs_exist_ok=True)


def build_image(
    image: str,
    context_dir: Path,
    *,
    assets_dir: Optional[Path] = None,
):
    """Build `image` from a staged context.

    The staged context is `context_dir` overlaid with `assets_dir` (defaults to
    this package's bundled docker/ scripts). Raises SetupException on failure.
    """
    context_dir = Path(context_dir)
    if not (context_dir / "Dockerfile").is_file():
        raise SetupException(f"No Dockerfile in build context {context_dir}.")
    if assets_dir is None:
        assets_dir = SHARED_DOCKER_ASSETS

    print(f"Building docker image {image} from {context_dir}...")
    with tempfile.TemporaryDirectory(prefix="devenv-ctx-") as tmp:
        staged = Path(tmp)
        _copy_into(context_dir, staged)
        if Path(assets_dir).is_dir():
            _copy_into(assets_dir, staged)
        cmd = ["docker", "build", "-t", image, str(staged)]
        try:
            subprocess.run(cmd, check=True)
        except subprocess.CalledProcessError as e:
            raise SetupException(f"Failed to build docker image {image}.") from e
    print(f"Successfully built docker image {image}.")


# ---- Run / exec --------------------------------------------------------

# Host CDI spec for NVIDIA GPUs, generated by `nvidia-ctk cdi generate`
# (offered by the wizard's setup_cdi step). When present we grant GPU access
# via CDI instead of the legacy `--gpus all` hook: the hook injects
# /dev/nvidia* behind systemd's back, so under cgroup v2 a suspend/resume or
# daemon-reload can silently revoke the GPU from a running container; CDI
# declares the devices in the container spec and is immune.
CDI_SPEC_PATH = Path("/etc/cdi/nvidia.yaml")


def cdi_spec_exists() -> bool:
    return CDI_SPEC_PATH.is_file()


def gpu_docker_args() -> list:
    """`docker run` args granting GPU access: CDI if available, else --gpus all."""
    if cdi_spec_exists():
        return ["--device", "nvidia.com/gpu=all"]
    return ["--gpus", "all"]


def is_container_running(name: str) -> bool:
    result = subprocess.run(
        ["docker", "inspect", "--format={{.State.Running}}", name],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
    )
    return result.returncode == 0 and result.stdout.strip().lower() == "true"


def _run_with_tmux_rename(cmd: list):
    """Run a long-lived interactive command, temporarily renaming the tmux
    window to "docker" (if we're inside tmux) so it's easy to spot."""
    old_name = None
    if "TMUX" in os.environ:
        old_name = subprocess.check_output(
            ["tmux", "display-message", "-p", "#W"], text=True
        ).strip()
        subprocess.run(["tmux", "rename-window", "docker"], check=False)
    try:
        print(f"$ {' '.join(cmd)}")
        subprocess.run(cmd, check=False)
    finally:
        if old_name is not None:
            subprocess.run(["tmux", "rename-window", old_name], check=False)


def exec_into_running(name: str, remote_user: str):
    cmd = ["docker", "exec", "-it", name, "gosu", remote_user, "bash"]
    _run_with_tmux_rename(cmd)


def _convenience_mounts() -> list:
    """Bind-mounts for per-user conveniences shared across all projects.

    These land at neutral paths under /workspace (outside /home, so they don't
    interfere with the entrypoint's useradd); devuser-setup.sh symlinks them
    into the dev user's home.
    """
    claude_dir = Path.home() / ".claude"
    claude_dir.mkdir(parents=True, exist_ok=True)
    # touch() prevents Docker from pre-creating these as root-owned
    # files/directories if they're missing on the host.
    claude_json = Path.home() / ".claude.json"
    claude_json.touch(exist_ok=True)
    gitconfig = Path.home() / ".gitconfig"
    gitconfig.touch(exist_ok=True)
    # VSCode installs its server + extensions into ~/.vscode-server inside the
    # container; because we run with --rm that whole tree (every extension) is
    # discarded on exit, forcing a reinstall on each relaunch. A dedicated host
    # dir persists it so extensions install once. It's its own dir (not the
    # host's real ~/.vscode-server) to avoid colliding with any VSCode the host
    # itself runs.
    vscode_server = Path.home() / ".devenv_vscode_server"
    vscode_server.mkdir(parents=True, exist_ok=True)
    # devuser-setup.sh generates the container's ssh identity (keypair, config,
    # known_hosts) in ~/.ssh; persisting that dir keeps the identity stable
    # across --rm relaunches, so an external machine that authorized the key
    # once stays reachable. A dedicated dir rather than the host's real ~/.ssh,
    # so the host's own keys are never exposed inside containers.
    #
    # One dir per host user, so every devenv_utils consumer on the machine
    # shares it -- deliberately: a machine authorizes the key once and every
    # project's container can reach it, and host aliases and known_hosts
    # accumulate in one place. The flip side is one identity and one alias
    # namespace across projects, so an alias means the same host everywhere.
    ssh_dir = Path.home() / ".devenv_ssh"
    ssh_dir.mkdir(parents=True, exist_ok=True)
    ssh_dir.chmod(0o700)  # ssh refuses group/world-accessible key material
    return [
        "-v", f"{claude_dir}:/workspace/.home-dir-soft-links/.claude",
        "-v", f"{claude_json}:/workspace/.home-dir-soft-links/.claude.json",
        "-v", f"{gitconfig}:/workspace/.home-dir-soft-links/.gitconfig",
        "-v", f"{vscode_server}:/workspace/.home-dir-soft-links/.vscode_server",
        "-v", f"{ssh_dir}:/workspace/.home-dir-soft-links/.ssh",
    ]


def run_container(
    config,
    *,
    image: str,
    instance_name: str,
    mount_dir: Optional[str] = None,
    extra_args: Optional[list] = None,
):
    """`docker run` a fresh container for `config`, dropping into a shell.

    Bind-mounts the repo (and the mount dir, if the project uses one) and passes
    the host UID/GID plus the workspace path so the container's entrypoint can
    reconcile ownership and the per-user setup can cd correctly. `extra_args`
    (from the project's pre-launch hook, plus the service-wiring args) are
    appended after the config's static extra_docker_args; any port publishing a
    project needs comes through there.
    """
    uid = subprocess.check_output(["id", "-u"], text=True).strip()
    gid = subprocess.check_output(["id", "-g"], text=True).strip()

    cmd = [
        "docker", "run", "--rm", "-it",
    ] + gpu_docker_args() + [
        "--name", instance_name,
        "--hostname", config.container_hostname,
        "-e", f"HOST_UID={uid}",
        "-e", f"HOST_GID={gid}",
        "-e", f"USERNAME={config.remote_user}",
        "-e", f"DEVENV_WORKSPACE={config.container_repo_path}",
        "-v", f"{config.repo_root}:{config.container_repo_path}",
    ]
    if config.container_mount_path is not None:
        assert mount_dir, "mount_dir required when config has a mount path"
        cmd += ["-v", f"{mount_dir}:{config.container_mount_path}"]
    cmd += _convenience_mounts()
    cmd += config.extra_docker_args
    if extra_args:
        cmd += extra_args
    cmd += [image, "bash"]

    _run_with_tmux_rename(cmd)
