"""devenv_utils: reusable building blocks for project dev environments.

Public API for constructing a project's host-side setup/run tooling on top of
a shared Docker-based dev container. A project supplies a `DevenvConfig` and
either drives `SetupWizardTool` (interactive first-time setup) or calls the
standalone helpers (build/run) directly.
"""

from .cli import docker_build, docker_launch
from .config import DevenvConfig, load_config
from .console import (
    SetupException,
    print_green,
    print_red,
    print_rule,
    yes_no,
)
from .dev_tool import DevTool
from .docker_ops import (
    build_image,
    cdi_spec_exists,
    exec_into_running,
    gpu_docker_args,
    image_exists,
    is_container_running,
    is_version_ok,
    major_version,
    parse_version_str,
    run_container,
)
from .download import download, have
from .nvidia import setup_cdi, validate_nvidia_driver, validate_nvidia_installation
from .state import (
    get_env_json,
    in_docker_container,
    is_subpath,
    update_env_json,
)
from .vscode_attach import (
    desired_vscode_attach_config,
    vscode_attach_config_paths,
    write_vscode_attach_config,
)
from .wizard import SetupWizardTool, check_setup_version

__all__ = [
    "DevenvConfig",
    "load_config",
    "DevTool",
    "SetupWizardTool",
    "check_setup_version",
    "SetupException",
    "docker_build",
    "docker_launch",
    "print_green",
    "print_red",
    "print_rule",
    "yes_no",
    "build_image",
    "cdi_spec_exists",
    "exec_into_running",
    "gpu_docker_args",
    "image_exists",
    "is_container_running",
    "is_version_ok",
    "major_version",
    "parse_version_str",
    "run_container",
    "download",
    "have",
    "setup_cdi",
    "validate_nvidia_driver",
    "validate_nvidia_installation",
    "get_env_json",
    "in_docker_container",
    "is_subpath",
    "update_env_json",
    "desired_vscode_attach_config",
    "vscode_attach_config_paths",
    "write_vscode_attach_config",
]
