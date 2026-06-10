"""Host-side state helpers: the per-repo .env.json and path/runtime probes.

The .env.json file persists user choices (e.g. the mount directory) between
runs of the setup/run scripts. It lives at the repo root by convention; callers
pass its path explicitly so this module stays project-agnostic.
"""

import json
import os
from pathlib import Path


def get_env_json(path: os.PathLike) -> dict:
    path = Path(path)
    if path.exists():
        with path.open() as f:
            return json.load(f)
    return {}


def update_env_json(path: os.PathLike, mappings: dict) -> None:
    path = Path(path)
    env = get_env_json(path)
    env.update(mappings)
    with path.open("w") as f:
        json.dump(env, f, indent=2)
        f.write("\n")


def is_subpath(child: os.PathLike, parent: os.PathLike) -> bool:
    return Path(child).resolve().is_relative_to(Path(parent).resolve())


def in_docker_container() -> bool:
    # Set by the Dockerfile via `ENV DOCKER_IMAGE_VERSION=...`.
    return "DOCKER_IMAGE_VERSION" in os.environ
