"""SetupWizardTool: orchestrates the generic, interactive first-time setup.

Projects subclass this to add project-specific steps (e.g. fetching data
files) and drive the steps from their own setup script. Every generic step is
a method here; project state that flows between steps (notably the chosen mount
directory) is held on the instance.
"""

import os
import subprocess
from pathlib import Path

from .config import DevenvConfig
from .console import SetupException, print_green, print_red, print_rule, yes_no
from .docker_ops import build_image
from .nvidia import setup_cdi, validate_nvidia_driver, validate_nvidia_installation
from .state import get_env_json, is_subpath, update_env_json
from .vscode_attach import (
    vscode_attach_config_paths,
    write_vscode_attach_config,
)


class SetupWizardTool:
    def __init__(self, config: DevenvConfig):
        self.config = config
        self.mount_dir: Path | None = None

    # ---- Step: mount dir ------------------------------------------------

    def setup_mount_dir(self) -> Path:
        c = self.config
        assert c.container_mount_path is not None, \
            f"{c.name} has no container_mount_path; skip the mount-dir step."
        print(f"{c.name} needs a persistent directory on the host that gets")
        print(f"bind-mounted into the Docker container at {c.container_mount_path}.")
        print("It holds data that must outlive any single container, and it MUST")
        print("live outside this repo.")
        print()

        env = get_env_json(c.env_json_path)
        default = env.get("MOUNT_DIR", str(c.default_mount_dir))

        while True:
            ans = input(f"Mount directory [{default}]: ").strip() or default
            target = os.path.abspath(os.path.expanduser(ans))
            if is_subpath(target, c.repo_root):
                print_red(f"Mount dir cannot live inside the repo ({c.repo_root}).")
                continue
            try:
                os.makedirs(target, exist_ok=True)
            except OSError as e:
                print_red(f"Could not create {target}: {e}")
                continue
            break

        update_env_json(c.env_json_path, {"MOUNT_DIR": target})
        print_green(f"Mount dir: {target}")
        self.mount_dir = Path(target)
        return self.mount_dir

    # ---- Step: docker permissions --------------------------------------

    def validate_docker_permissions(self):
        print("Checking that you can run `docker` without sudo...")
        result = subprocess.run(
            ["docker", "ps"],
            stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True,
        )
        if result.returncode == 0:
            print_green("Docker is usable without sudo.")
            return

        if "permission denied" in result.stderr.lower():
            print_red("You can't run docker without sudo. Add yourself to the docker group:")
            print("    sudo usermod -aG docker $USER")
            print("Then log out and back in.")
        else:
            print_red("`docker ps` failed:")
            print(result.stderr)
        raise SetupException()

    # ---- Step: VS Code attach config -----------------------------------

    def setup_vscode_attach_config(self):
        c = self.config
        paths = vscode_attach_config_paths(c.instance_name)
        if not paths:
            print("No VS Code user-data directory found for any flavor")
            print("(Code / Code - Insiders / VSCodium). Skipping VS Code attach")
            print("config. If you install VS Code later, re-run this wizard, or")
            print("manually use 'Dev Containers: Open Named Container Configuration")
            print(f"File' and set \"remoteUser\": \"{c.remote_user}\".")
            return

        print("VS Code's 'Attach to Running Container' command does NOT read")
        print("this repo's .devcontainer/devcontainer.json. It reads a per-container")
        print("config under the VS Code user-data dir. Without it, vscode-server")
        print("runs as root inside the container.")
        print()
        print("Detected user-data dir(s); proposing to write/merge:")
        for p in paths:
            print(f"  {p}")
        print()
        if not yes_no(f"Configure VS Code attach to run as {c.remote_user}?"):
            print("Skipping VS Code attach config.")
            return

        for p in paths:
            try:
                status = write_vscode_attach_config(
                    p, c.instance_name, c.remote_user, c.container_repo_path
                )
            except RuntimeError as e:
                print_red(str(e))
                continue
            if status == "created":
                print_green(f"Wrote {p}")
            elif status == "updated":
                print_green(f"Updated {p} (merged in remoteUser/workspaceFolder/containerName)")
            else:
                print(f"{p} already up to date.")

    # ---- Step: build image ---------------------------------------------

    def build_docker_image(self, context: os.PathLike | None = None,
                           version: str | None = None):
        c = self.config
        build_image(c.image, context or c.docker_context, version=version)

    # ---- Step: NVIDIA --------------------------------------------------

    def validate_nvidia_driver(self):
        validate_nvidia_driver()

    def validate_nvidia_installation(self, image: str | None = None):
        validate_nvidia_installation(image or self.config.image)

    def setup_cdi(self):
        setup_cdi()

    # ---- Convenience ----------------------------------------------------

    def rule(self):
        print_rule()
