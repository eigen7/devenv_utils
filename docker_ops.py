"""Docker build/run helpers shared by the devenv setup/run scripts.

`docker_build` stages a temporary build context: it copies the project's own
docker context (Dockerfile + any project files) and then overlays the shared
scripts that ship with this package (entrypoint.sh, devuser-setup.sh). This
lets each project keep a minimal Dockerfile that `COPY`s the shared scripts
without those scripts having to physically live in the project tree.
"""

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


# ---- Image version labels ----------------------------------------------

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


def get_image_label(image: str, label_key: str) -> Optional[str]:
    """Return the value of a single LABEL on a local Docker image, or None."""
    try:
        result = subprocess.check_output(
            [
                "docker", "inspect",
                f"--format={{{{index .Config.Labels \"{label_key}\"}}}}",
                image,
            ],
            stderr=subprocess.STDOUT,
            text=True,
        ).strip()
    except subprocess.CalledProcessError:
        return None
    return result or None


def image_exists(image: str) -> bool:
    return subprocess.run(
        ["docker", "image", "inspect", image],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    ).returncode == 0


def check_image_version(image: str, minimum: str) -> bool:
    """Print a diagnostic and return whether `image`'s version label is OK."""
    version = get_image_label(image, "version")
    if is_version_ok(version, minimum):
        return True
    if version is None:
        print("Image is missing a `version` label; it may be out of date.")
    else:
        print(f"Image version {version} < required {minimum}.")
    return False


# ---- Build -------------------------------------------------------------

def _copy_into(src: Path, dst: Path) -> None:
    """Copy the *contents* of directory `src` into existing directory `dst`."""
    shutil.copytree(src, dst, dirs_exist_ok=True)


def build_image(
    image: str,
    context_dir: Path,
    *,
    assets_dir: Optional[Path] = None,
    version: Optional[str] = None,
) -> None:
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
        cmd = ["docker", "build", "-t", image]
        if version is not None:
            cmd += ["--build-arg", f"VERSION={version}"]
        cmd += [str(staged)]
        try:
            subprocess.run(cmd, check=True)
        except subprocess.CalledProcessError as e:
            raise SetupException(f"Failed to build docker image {image}.") from e
    print(f"Successfully built docker image {image}.")


# ---- Run / exec --------------------------------------------------------

def is_container_running(name: str) -> bool:
    result = subprocess.run(
        ["docker", "inspect", "--format={{.State.Running}}", name],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
    )
    return result.returncode == 0 and result.stdout.strip().lower() == "true"


def exec_into_running(name: str, remote_user: str) -> None:
    cmd = ["docker", "exec", "-it", name, "gosu", remote_user, "bash"]
    print(f"$ {' '.join(cmd)}")
    subprocess.run(cmd, check=False)


def run_container(config, *, image: str, instance_name: str, mount_dir: str) -> None:
    """`docker run` a fresh container for `config`, dropping into a shell.

    Bind-mounts the repo and mount dir, forwards the configured ports, and
    passes the host UID/GID plus the workspace path so the container's
    entrypoint can reconcile ownership and the per-user setup can cd correctly.
    """
    uid = subprocess.check_output(["id", "-u"], text=True).strip()
    gid = subprocess.check_output(["id", "-g"], text=True).strip()

    cmd = [
        "docker", "run", "--rm", "-it",
        "--gpus", "all",
        "--name", instance_name,
        "-e", f"HOST_UID={uid}",
        "-e", f"HOST_GID={gid}",
        "-e", f"USERNAME={config.remote_user}",
        "-e", f"DEVENV_WORKSPACE={config.container_repo_path}",
        "-v", f"{config.repo_root}:{config.container_repo_path}",
        "-v", f"{mount_dir}:{config.container_mount_path}",
    ]
    for port in config.required_ports:
        cmd += ["-p", f"{port}:{port}"]
    cmd += [image, "bash"]

    print(f"$ {' '.join(cmd)}")
    subprocess.run(cmd, check=False)
