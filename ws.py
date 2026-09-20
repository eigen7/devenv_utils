#!/usr/bin/env python3
"""Drive a workshop: the repo that assembles component repos (WORKSHOPS.md).

Run this from the workshop repo, which vendors devenv_utils as a subtree:

  ws.py setup
      Bring the workshop up on this machine: clone every component named in
      workshop.toml, create xfer/, point each clone's Claude Code settings at
      the workshop's shared memory, and offer to run each component's
      setup_wizard.py. Idempotent -- rerun it after adding a component, and
      it only does what is missing. This is the one command a collaborator
      runs after cloning the workshop repo.

  ws.py status
      Per component: whether it is cloned, its branch and dirty state, whether
      its vendored devenv_utils is new enough to detect the workshop, whether
      it has been set up (.env.json), and whether its container is running.

Both read workshop.toml from the workshop root, found by walking up from the
working directory.
"""

import sys
from pathlib import Path

if __package__ in (None, ""):
    # Enable running this file directly, the same way pr_flow.py does: load the
    # package under its canonical name from this file's own directory, whatever
    # that directory is called.
    import importlib.util

    _pkg_dir = Path(__file__).resolve().parent
    _spec = importlib.util.spec_from_file_location(
        "devenv_utils",
        _pkg_dir / "__init__.py",
        submodule_search_locations=[str(_pkg_dir)],
    )
    _pkg = importlib.util.module_from_spec(_spec)
    sys.modules["devenv_utils"] = _pkg
    _spec.loader.exec_module(_pkg)
    __package__ = "devenv_utils"

import argparse
import json
import subprocess

from .config import load_config
from .console import (
    SetupException,
    print_green,
    print_red,
    print_rule,
    yes_no,
)
from .docker_ops import is_container_running
from .state import get_env_json, in_docker_container
from .workshop import MANIFEST, Component, Workshop, load_workshop

# Claude Code reads this per-directory settings file but ignores the key in the
# checked-in settings.json, so the shared-memory pointer has to live here. The
# file is per machine and gitignored by every consumer.
CLAUDE_LOCAL_SETTINGS = Path(".claude/settings.local.json")


def find_workshop_root(start: Path) -> Path:
    """The nearest directory at or above `start` holding a workshop manifest."""
    for candidate in [start, *start.parents]:
        if (candidate / MANIFEST).is_file():
            return candidate
    raise SetupException(
        f"No {MANIFEST} in {start} or any parent. Run this from a workshop repo (WORKSHOPS.md)."
    )


def git(*args: str, cwd: Path | None = None) -> subprocess.CompletedProcess:
    return subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True, check=False)


def clone(component: Component):
    """Clone a component that isn't there yet. Without a `branch` in the
    manifest, git takes the remote's default branch, whatever it is called."""
    cmd = ["git", "clone"]
    if component.branch:
        cmd += ["--branch", component.branch]
    cmd += [component.url, str(component.path)]
    print(f"Cloning {component.key} from {component.url} ...")
    if subprocess.run(cmd, check=False).returncode != 0:
        raise SetupException(
            f"Cloning {component.key} failed. Check that you can reach {component.url} "
            "(the workshop uses your own git credentials)."
        )


def check_origin(component: Component):
    """Warn when an existing clone points somewhere other than the manifest."""
    result = git("remote", "get-url", "origin", cwd=component.path)
    origin = result.stdout.strip()
    if result.returncode == 0 and origin and origin != component.url:
        print_red(
            f"{component.key}: origin is {origin}, but {MANIFEST} says {component.url}. "
            "Leaving the clone alone."
        )


def vendored_supports_workshops(component: Component) -> bool:
    """Whether the component's vendored devenv_utils can detect a workshop."""
    return (component.path / "subtrees/devenv_utils/workshop.py").is_file()


def write_claude_memory_setting(workshop: Workshop, directory: Path) -> bool:
    """Point one directory's Claude Code settings at the workshop's shared memory
    directory, leaving any other local settings untouched. True if it changed."""
    path = directory / CLAUDE_LOCAL_SETTINGS
    wanted = f"~/.claude/projects/{workshop.name}/memory"
    settings = {}
    if path.is_file():
        try:
            settings = json.loads(path.read_text())
        except json.JSONDecodeError:
            print_red(f"{path} is not valid JSON; not touching it.")
            return False
    if settings.get("autoMemoryDirectory") == wanted:
        return False
    settings["autoMemoryDirectory"] = wanted
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(settings, indent=2) + "\n")
    return True


def run_wizard(component: Component):
    """Run a component's setup_wizard.py interactively, if the user wants it."""
    wizard = component.path / "setup_wizard.py"
    if not wizard.is_file():
        print_red(f"{component.key}: no setup_wizard.py; skipping setup.")
        return
    already = "SETUP_VERSION" in get_env_json(component.path / ".env.json")
    prompt = (
        f"Re-run {component.key}'s setup wizard?" if already else f"Run it for {component.key}?"
    )
    if not yes_no(prompt, default_yes=not already):
        return
    subprocess.run([sys.executable, str(wizard)], cwd=component.path, check=False)


def setup(workshop: Workshop):
    """Clone what's missing, wire up the shared bits, then offer each wizard."""
    (workshop.root / "xfer").mkdir(exist_ok=True)
    if write_claude_memory_setting(workshop, workshop.root):
        print(f"Claude memory for the workshop -> ~/.claude/projects/{workshop.name}/memory")

    for component in workshop.components:
        print_rule()
        print(f"{component.key}")
        if not component.path.exists():
            clone(component)
        else:
            print(f"Already cloned at {component.path}.")
            check_origin(component)
        write_claude_memory_setting(workshop, component.path)
        if not component.container:
            continue
        if not (component.path / "devenv.toml").is_file():
            print(f"{component.key}: no devenv.toml, so no dev container.")
            continue
        if not vendored_supports_workshops(component):
            print_red(
                f"{component.key}: its vendored devenv_utils predates workshops, so it would "
                "run standalone (its own container name, image tag and mount dir).\n"
                "Land a subtrees/devenv_utils update there (pull_subtrees.py) before setting "
                "it up."
            )
            continue
        config = load_config(component.path)
        print(
            f"container {config.instance_name}, image {config.image}, "
            f"mount {get_env_json(config.env_json_path).get('MOUNT_DIR', config.default_mount_dir)}"
        )
        run_wizard(component)

    print_rule()
    print_green(f"{workshop.name} is set up.")
    print("Launch a component's dev container with its own ./run_docker.py.")


def status(workshop: Workshop):
    """One line per component: where it is and whether it is ready."""
    print(f"{workshop.name}  ({workshop.root})")
    for component in workshop.components:
        if not component.path.exists():
            print(f"  {component.key}: not cloned")
            continue
        branch = git("rev-parse", "--abbrev-ref", "HEAD", cwd=component.path).stdout.strip()
        dirty = bool(git("status", "--porcelain", cwd=component.path).stdout.strip())
        notes = [branch + ("*" if dirty else "")]
        if not component.container:
            notes.append("no container")
        elif not vendored_supports_workshops(component):
            notes.append("devenv_utils predates workshops")
        else:
            config = load_config(component.path)
            notes.append("set up" if get_env_json(config.env_json_path) else "not set up")
            if is_container_running(config.instance_name):
                notes.append(f"{config.instance_name} running")
        print(f"  {component.key}: {', '.join(notes)}")


def main():
    parser = argparse.ArgumentParser(description="Drive a workshop of component repos.")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("setup", help="clone components, wire up the workshop, run their wizards")
    sub.add_parser("status", help="per-component branch, setup and container state")
    args = parser.parse_args()

    assert not in_docker_container(), (
        "ws runs on the host: it clones repos and drives Docker for every component."
    )
    try:
        workshop = load_workshop(find_workshop_root(Path.cwd()))
        {"setup": setup, "status": status}[args.command](workshop)
    except SetupException as e:
        for arg in e.args:
            print_red(str(arg))
        sys.exit(1)


if __name__ == "__main__":
    main()
