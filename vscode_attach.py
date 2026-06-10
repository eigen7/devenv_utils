"""VS Code "Attach to Running Container" config management.

Background: when you use the Dev Containers extension's "Attach to Running
Container" command, VS Code does NOT consult .devcontainer/devcontainer.json
in the workspace. Instead it reads a per-container config from the user's
globalStorage:

  <vscode-user-data>/User/globalStorage/
      ms-vscode-remote.remote-containers/nameConfigs/<container>.json

Without that file, VS Code has no way to know which user to attach as, and
defaults to whatever USER the image declares. For images that create their
dev user at container start (not image-build time), that's root. Writing this
file at setup time makes attach Just Work as the intended remote user.
"""

import json
import os
import sys
from pathlib import Path

# Subpath under each VS Code flavor's user-data dir.
_NAMECONFIGS_SUBPATH = Path(
    "User", "globalStorage", "ms-vscode-remote.remote-containers", "nameConfigs"
)


def _vscode_user_data_roots() -> list[Path]:
    """Candidate user-data roots for installed VS Code flavors on this OS.

    We return one path per flavor (Code, Code - Insiders, VSCodium) regardless
    of whether it actually exists; the caller decides what to do with missing
    directories. Order: stable, insiders, vscodium.
    """
    flavors = ["Code", "Code - Insiders", "VSCodium"]
    if sys.platform == "darwin":
        base = Path.home() / "Library" / "Application Support"
    elif sys.platform.startswith("win"):
        appdata = os.environ.get("APPDATA")
        if not appdata:
            return []
        base = Path(appdata)
    else:
        # Linux / *BSD: honor XDG_CONFIG_HOME, default to ~/.config.
        base = Path(os.environ.get("XDG_CONFIG_HOME") or (Path.home() / ".config"))
    return [base / flavor for flavor in flavors]


def vscode_attach_config_paths(instance_name: str) -> list[Path]:
    """Paths to nameConfigs/<instance>.json for every VS Code flavor present.

    A flavor is considered "present" if its user-data root directory already
    exists on disk (we don't want to materialize a config dir for an editor the
    user doesn't use).
    """
    paths: list[Path] = []
    for root in _vscode_user_data_roots():
        if root.is_dir():
            paths.append(root / _NAMECONFIGS_SUBPATH / f"{instance_name}.json")
    return paths


def desired_vscode_attach_config(
    instance_name: str, remote_user: str, workspace_folder: str
) -> dict:
    """Minimal attach config we want to ensure is present."""
    return {
        "containerName": instance_name,
        "remoteUser": remote_user,
        "workspaceFolder": workspace_folder,
    }


def write_vscode_attach_config(
    path: Path, instance_name: str, remote_user: str, workspace_folder: str
) -> str:
    """Create or merge the attach config at `path`. Returns a status string.

    - If `path` does not exist: write the desired config.
    - If `path` exists: merge our keys in, preserving any extra keys (e.g.
      `extensions`, `settings`) the user has added by hand. We overwrite our
      three keys if they differ, since their whole purpose is to make attach
      work correctly.

    Returns one of: "created", "updated", "unchanged".
    """
    desired = desired_vscode_attach_config(instance_name, remote_user, workspace_folder)
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)

    if path.exists():
        try:
            existing = json.loads(path.read_text())
            if not isinstance(existing, dict):
                raise ValueError(f"top-level JSON in {path} is not an object")
        except (json.JSONDecodeError, ValueError) as e:
            raise RuntimeError(
                f"Refusing to overwrite {path}: it exists but is not valid JSON "
                f"({e}). Fix or delete it and re-run."
            )
        merged = dict(existing)
        merged.update(desired)
        if merged == existing:
            return "unchanged"
        path.write_text(json.dumps(merged, indent=4) + "\n")
        return "updated"

    path.write_text(json.dumps(desired, indent=4) + "\n")
    return "created"
