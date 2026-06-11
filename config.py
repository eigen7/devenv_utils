"""DevenvConfig: the per-project knobs the generic devenv tooling needs.

A project constructs one of these (typically in its own small config module)
and hands it to SetupWizardTool and to the standalone run/build scripts. Only
`name` and `repo_root` are required; everything else derives a sensible default
from those, and can be overridden per project.
"""

from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional


@dataclass
class DevenvConfig:
    name: str
    repo_root: Path

    # Local Docker image tag. Defaults to `name`.
    image: str = ""
    # Container name used by run_docker / the VS Code attach config.
    # Defaults to f"{name}_instance".
    instance_name: str = ""
    # Hostname inside the container. Defaults to f"{name}-container".
    container_hostname: str = ""
    # Suggested host mount dir during setup. Defaults to ~/<name>.
    default_mount_dir: Optional[Path] = None

    # Where the repo / mount dir are bind-mounted inside the container.
    # Set container_mount_path to None for projects that don't need a
    # persistent mount dir: the wizard's mount-dir step and run_docker's
    # MOUNT_DIR requirement are then skipped entirely.
    container_repo_path: str = "/workspace/repo"
    container_mount_path: Optional[str] = "/workspace/mount"

    # Ports forwarded host -> container by run_docker.
    required_ports: list[int] = field(default_factory=list)
    # Extra static args appended to every `docker run` (e.g. ["--ipc=host"]).
    extra_docker_args: list[str] = field(default_factory=list)
    # Minimum acceptable value of the image's `version` label.
    min_image_version: str = "0.0.0"
    # Unprivileged user the container runs as / VS Code attaches as.
    remote_user: str = "devuser"

    # Docker build context (holds the project Dockerfile). Defaults to
    # <repo_root>/docker-setup.
    docker_context: Optional[Path] = None
    # Where persisted user choices live. Defaults to <repo_root>/.env.json.
    env_json_path: Optional[Path] = None

    def __post_init__(self) -> None:
        self.repo_root = Path(self.repo_root)
        if not self.image:
            self.image = self.name
        if not self.instance_name:
            self.instance_name = f"{self.name}_instance"
        if not self.container_hostname:
            self.container_hostname = f"{self.name}-container"
        if self.default_mount_dir is None:
            self.default_mount_dir = Path.home() / self.name
        self.default_mount_dir = Path(self.default_mount_dir)
        if self.docker_context is None:
            self.docker_context = self.repo_root / "docker-setup"
        self.docker_context = Path(self.docker_context)
        if self.env_json_path is None:
            self.env_json_path = self.repo_root / ".env.json"
        self.env_json_path = Path(self.env_json_path)
