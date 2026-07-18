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

from . import gateway_service
from .config import DevenvConfig
from .console import SetupException
from .docker_ops import (
    build_image,
    exec_into_running,
    is_container_running,
    run_container,
)
from .gitea_service import dev_container_args
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

    Each service in the project's `[services]` table is routed by the gateway at
    http://<project>-<service>.localhost; the service -> URL table is printed at
    every launch and attach (see GATEWAY.md).

    Hooks for project-specific behavior:
      extend_parser(parser): add project CLI flags before parsing.
      pre_launch(args) -> list[str]: runs on the host just before `docker run`
        for a fresh container; returns extra `docker run` args (e.g. device
        passthrough, networking flags). Host-side side effects (loading kernel
        modules, ...) belong here too. Not called when exec'ing into an
        already-running container.
    """
    env = get_env_json(config.env_json_path)

    parser = argparse.ArgumentParser(
        description=f"Launch (or attach to) the {config.name} dev container."
    )
    parser.add_argument("-d", "--docker-image",
                        help=f"image to run (default: {config.image})")
    parser.add_argument("-i", "--instance-name", default=config.instance_name,
                        help="container name (default: %(default)s)")
    if extend_parser is not None:
        extend_parser(parser)
    args = parser.parse_args()

    if is_container_running(args.instance_name):
        print(f"Container {args.instance_name} already running; exec'ing in.")
        # The container's network mode is fixed at its original launch, and the
        # gateway routes are attached then too; here we only reprint the table.
        _print_service_urls(config, "--network=host" in config.extra_docker_args)
        exec_into_running(args.instance_name, config.remote_user)
        return

    _launch_fresh(config, args, env, pre_launch)


def _print_service_urls(config: DevenvConfig, host_network: bool):
    """Print the aligned service -> URL table (nothing when the project declares
    no services). Exits on a gateway wiring error, like the launch path."""
    try:
        urls = gateway_service.launch_urls(config, host_network)
    except SetupException as e:
        print(e)
        sys.exit(1)
    if not urls:
        return
    width = max(len(name) for name in urls)
    print("Service URLs:")
    for name, url in urls.items():
        line = f"  {name.ljust(width)}  {url}"
        service = config.services[name]
        if service.publish and not host_network:
            line += f"  (also published at 127.0.0.1:{service.port})"
        print(line)


def _launch_fresh(
    config: DevenvConfig,
    args: argparse.Namespace,
    env: dict,
    pre_launch: Optional[Callable[[argparse.Namespace], list]] = None,
):
    image = args.docker_image or env.get("DOCKER_IMAGE") or config.image

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

    extra_args = pre_launch(args) if pre_launch is not None else []

    # Wire the dev container up to the shared services -- and start them if they
    # are stopped: the Gitea service (network membership, env contract,
    # credentials mount) and the gateway (routing labels, published ports, and
    # DEVENV_SERVICE_URL_* env derived from the [services] table).
    host_network = "--network=host" in config.extra_docker_args + extra_args
    try:
        extra_args = extra_args + dev_container_args(host_network)
        extra_args = extra_args + gateway_service.dev_container_args(config, host_network)
    except SetupException as e:
        print(e)
        sys.exit(1)

    _print_service_urls(config, host_network)

    run_container(
        config, image=image, instance_name=args.instance_name,
        mount_dir=mount_dir, extra_args=extra_args,
    )
