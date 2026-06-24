"""High-level CLI entry points.

A project's build/run scripts call these with just a `DevenvConfig`, so each
script's `main()` is a single line. These functions own the host-side checks,
argument parsing, and process exit handling; the actual work is delegated to
the primitives in `docker_ops`.
"""

import argparse
import os
import sys
from typing import Callable, Optional

from .config import DevenvConfig
from .console import SetupException
from .docker_ops import (
    build_image,
    check_image_version,
    exec_into_running,
    is_container_running,
    run_container,
)
from .instances import (
    assert_no_port_conflicts,
    instance_number,
    instanced_name,
    port_offset,
)
from .state import get_env_json, in_docker_container, is_subpath


def docker_build(config: DevenvConfig):
    """Build the project's local Docker image. Entry point for build scripts."""
    assert not in_docker_container(), \
        "the image build must run on the host, not inside the container."
    parser = argparse.ArgumentParser(
        description=f"Build the local {config.name} Docker image."
    )
    parser.add_argument(
        "-l", "--local-docker-image", default=config.image,
        help="local image tag (default: %(default)s)",
    )
    args = parser.parse_args()
    try:
        build_image(args.local_docker_image, config.docker_context)
    except SetupException as e:
        for arg in e.args:
            print(arg)
        sys.exit(1)


def docker_launch(
    config: DevenvConfig,
    *,
    extend_parser: Optional[Callable[[argparse.ArgumentParser], None]] = None,
    pre_launch: Optional[Callable[[argparse.Namespace], list]] = None,
):
    """Launch (or attach to) the project's dev container. Entry point for run scripts.

    The "INSTANCE" key in .env.json (default 0) selects a parallel instance:
    instance N gets its own container name and forwards its ports shifted up by
    instance_port_stride * N, so a second clone of the repo can run a container
    alongside the first. See instances.py.

    Hooks for project-specific behavior:
      extend_parser(parser): add project CLI flags before parsing.
      pre_launch(args) -> list[str]: runs on the host just before `docker run`
        for a fresh container; returns extra `docker run` args (e.g. device
        passthrough, networking flags). Host-side side effects (loading kernel
        modules, ...) belong here too. Not called when exec'ing into an
        already-running container.
    """
    env = get_env_json(config.env_json_path)
    instance = instance_number(env)

    parser = argparse.ArgumentParser(
        description=f"Launch (or attach to) the {config.name} dev container."
    )
    parser.add_argument("-d", "--docker-image",
                        help=f"image to run (default: {config.image})")
    parser.add_argument("-i", "--instance-name",
                        default=instanced_name(config.instance_name, instance),
                        help="container name (default: %(default)s)")
    parser.add_argument("-s", "--skip-image-version-check", action="store_true",
                        help="skip the image-version label check")
    if extend_parser is not None:
        extend_parser(parser)
    args = parser.parse_args()

    if is_container_running(args.instance_name):
        print(f"Container {args.instance_name} already running; exec'ing in.")
        exec_into_running(args.instance_name, config.remote_user)
        return

    _launch_fresh(config, args, env, instance, pre_launch)


def _launch_fresh(
    config: DevenvConfig,
    args: argparse.Namespace,
    env: dict,
    instance: int,
    pre_launch: Optional[Callable[[argparse.Namespace], list]] = None,
):
    image = args.docker_image or env.get("DOCKER_IMAGE") or config.image

    try:
        assert_no_port_conflicts(
            config.required_ports, instance, config.instance_port_stride
        )
    except SetupException as e:
        print(e)
        sys.exit(1)
    offset = port_offset(instance, config.instance_port_stride)

    mount_dir = None
    if config.container_mount_path is not None:
        mount_dir = env.get("MOUNT_DIR")
        if not mount_dir:
            print("Error: MOUNT_DIR is not set. Run ./setup_wizard.py first.")
            sys.exit(1)
        if not os.path.isdir(mount_dir):
            print(f"Error: mount dir {mount_dir} does not exist. Re-run ./setup_wizard.py.")
            sys.exit(1)
        assert not is_subpath(mount_dir, config.repo_root), \
            f"Mount dir {mount_dir} must not live inside repo {config.repo_root}"

    if not args.skip_image_version_check and not check_image_version(
        image, config.min_image_version
    ):
        print("Run ./build_docker_image.py to rebuild, "
              "or pass --skip-image-version-check.")
        sys.exit(1)

    extra_args = pre_launch(args) if pre_launch is not None else None

    run_container(
        config, image=image, instance_name=args.instance_name,
        mount_dir=mount_dir, extra_args=extra_args, port_offset=offset,
        instance=instance
    )
