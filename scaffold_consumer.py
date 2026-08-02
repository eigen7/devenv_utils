#!/usr/bin/env python3
"""Scaffold the thin consumer-side glue for a repo that vendors devenv_utils.

Run from the consumer repo root, AFTER adding the subtree:

    git subtree add --prefix subtrees/devenv_utils \\
        https://github.com/eigen7/devenv_utils.git main --squash
    python3 subtrees/devenv_utils/scaffold_consumer.py

It writes the generic glue (never overwriting an existing file):

    subtrees/__init__.py      package marker so `subtrees.devenv_utils` imports
    subtrees/README.md        pointer to the vendored-subtree rules
    py/setup_check.py         import bridge for py/ entrypoints
    devenv.toml               project config TEMPLATE (fill in)
    setup_common.py           loads devenv.toml + any project-specific constants
    setup_wizard.py           interactive first-time setup (extend with custom steps)
    build_docker_image.py     builds the local Docker image
    run_docker.py             launches (or attaches to) the dev container

The PR-workflow tools need no per-project shim: run them straight from the
subtree (subtrees/devenv_utils/pr_flow.py, stale_worktrees.py) -- each reads
the invoking repo's devenv.toml itself.

Then: fill in devenv.toml, write docker-setup/Dockerfile, and point your
CLAUDE.md at subtrees/devenv_utils/WORKFLOW.md. See CONSUMER_SETUP.md.
"""

import sys
from pathlib import Path

# This script lives at <repo>/subtrees/devenv_utils/scaffold_consumer.py.
REPO_ROOT = Path(__file__).resolve().parents[2]

_FILES = {
    "subtrees/__init__.py": '''\
"""Package root for the vendored subtrees under subtrees/ (see README.md).

This file exists so vendored packages can be imported as `subtrees.<name>`
from the repo root. It is a project-owned file, not part of any subtree.
"""
''',
    "subtrees/README.md": """\
This directory contains vendored git subtrees: read-only copies of repos we
control, updated by `git subtree pull` and never edited in place (a
pre-commit hook enforces this). The rules -- updating a copy, changing the
source repo, coordinated changes -- are documented in
[devenv_utils/SUBTREES.md](devenv_utils/SUBTREES.md). Read it before touching
anything under this directory.
""",
    "py/setup_check.py": '''\
"""Import the repo-root `setup_common` from the py/ entrypoints.

In the dev container the py/ scripts run with py/ -- not the repo root -- on
PYTHONPATH, so a plain `import setup_common` fails. Call import_setup_common()
instead; it works on the host too.
"""


def import_setup_common():
    """Import and return the repo-root `setup_common` module."""
    import sys
    from pathlib import Path

    repo_root = str(Path(__file__).resolve().parent.parent)
    if repo_root not in sys.path:
        sys.path.insert(0, repo_root)
    import setup_common
    return setup_common
''',
    "devenv.toml": """\
# Declarative devenv configuration, read by subtrees/devenv_utils
# load_config() into a DevenvConfig. Every key is a DevenvConfig field;
# path-valued keys are resolved relative to this file's directory.

name = "CHANGE_ME"

# Bump to force users to rerun the setup wizard, e.g. after a Dockerfile change
# that needs an image rebuild (a major -- first-number -- bump also wipes
# target/).
setup_version = "1.0.0"

# HTTP services run_docker.py should route through the gateway, as
# http://<name>-<service>.localhost (see subtrees/devenv_utils/GATEWAY.md).
# Each value is a container port, or {port, publish} to also publish
# 127.0.0.1:<port> for non-HTTP traffic.
# [services]
# web = 5173
""",
    "setup_common.py": '''\
"""Project-specific devenv configuration for THIS project.

The generic host-side machinery lives in the vendored `subtrees/devenv_utils`
copy. The static DevenvConfig fields live as data in the repo-root
`devenv.toml`; this module loads them and is where any project-specific
constants go. It lives at the repo root so host-side scripts can import it
without PYTHONPATH.
"""

from pathlib import Path

from subtrees.devenv_utils import (
    DevenvConfig,
    DevTool,
    load_config,
    check_setup_version as _check_setup_version,
)

REPO_ROOT = Path(__file__).resolve().parent


def check_setup_version():
    _check_setup_version(make_config())


def dev_tool() -> DevTool:
    """Return the project's dev-workflow helper (clang-format)."""
    return DevTool(make_config())


def make_config() -> DevenvConfig:
    """The project DevenvConfig, loaded from the repo-root devenv.toml."""
    return load_config(REPO_ROOT)
''',
    "setup_wizard.py": '''\
#!/usr/bin/env python3
"""Interactive first-time setup for this project.

Run this *outside* the Docker container. It:
  1. Installs the workflow git hooks (the read-only guard for subtrees/, the
     pre-push guard for main; see subtrees/devenv_utils/WORKFLOW.md).
  2. Picks the persistent host directory bind-mounted at /workspace/mount.
  3. Verifies you can run `docker` without sudo, on a new enough daemon.
  4. Provisions the GitHub token the dev container uses to push feature
     branches and open PRs (optional; see subtrees/devenv_utils/github_access.py).
  5. Provisions the devenv_utils working clone under the mount (see
     subtrees/devenv_utils/SUBTREES.md).
  6. Provisions the machine-wide gateway service that routes each project's
     http://<project>-<service>.localhost dev URLs (see
     subtrees/devenv_utils/GATEWAY.md).
  7. Writes a per-container VS Code config so that "Dev Containers: Attach
     to Running Container" connects as devuser instead of root.
  8. Pre-trusts the container workspace paths in the host Claude Code config.
  9. Builds the Docker image.

The generic steps live in `subtrees/devenv_utils`; project-specific steps
belong on the SetupWizard subclass below.

Re-run the wizard any time you want to refresh the VS Code attach config or
rebuild the image.
"""

import argparse
import os
import sys

from setup_common import make_config
from subtrees.devenv_utils import (
    SetupException,
    SetupWizardTool,
    in_docker_container,
    print_green,
)


class SetupWizard(SetupWizardTool):
    """Project-specific setup on top of the generic SetupWizardTool steps.

    Add a method here per custom step (fetching data files, writing
    credential templates, ...) and call it from main() between the generic
    steps, with tool.rule() separators.
    """


def get_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    return parser.parse_args()


def main():
    assert not in_docker_container(), (
        "setup_wizard.py is intended to be run on the host, not inside the container."
    )
    get_args()  # for --help

    config = make_config()
    os.chdir(config.repo_root)
    tool = SetupWizard(config)

    print("*" * 78)
    print(f"{config.name} setup wizard")
    print("*" * 78)

    try:
        tool.rm_target_on_major_bump()
        tool.rule()
        tool.setup_git_config()
        tool.rule()
        tool.setup_mount_dir()
        tool.rule()
        tool.validate_docker_permissions()
        tool.rule()
        tool.validate_docker_version()
        tool.rule()
        tool.setup_github_access()
        tool.rule()
        tool.setup_devenv_clone()
        tool.rule()
        tool.setup_gateway_service()
        tool.rule()
        tool.setup_vscode_attach_config()
        tool.rule()
        tool.setup_claude_trust()
        tool.rule()
        # Project-specific steps (methods on SetupWizard above) go here.
        tool.build_docker_image()
        tool.rule()
        # Stamp the setup version last, so it records only a fully completed
        # run. The entrypoints check this before doing any work.
        tool.commit()
        print_green("Setup complete.")
        print("Next: ./run_docker.py")
    except KeyboardInterrupt:
        print()
        print("Setup wizard interrupted. Re-run when ready.")
        sys.exit(1)
    except SetupException as e:
        for arg in e.args:
            print("*" * 78)
            print(arg)
        sys.exit(1)


if __name__ == "__main__":
    main()
''',
    "build_docker_image.py": '''\
#!/usr/bin/env python3
"""Build the local Docker image from docker-setup/ (devenv.toml docker_context)."""

from setup_common import check_setup_version, make_config
from subtrees.devenv_utils import docker_build


def main():
    check_setup_version()
    docker_build(make_config())


if __name__ == "__main__":
    main()
''',
    "run_docker.py": '''\
#!/usr/bin/env python3
"""Launch (or attach to) the dev container.

All launch machinery (repo bind-mount, the persistent /workspace/mount host
directory, UID/GID mapping, service wiring, exec-into-running) lives in
subtrees/devenv_utils, driven by the repo-root devenv.toml. Drops you into
a bash shell inside the container as `devuser`, whose UID/GID match your host
user, so files written into the bind-mounts are owned by you on the host.

Each service in devenv.toml's [services] table is reached through the gateway
at http://<project>-<service>.localhost; the launcher prints the service -> URL
table (see subtrees/devenv_utils/GATEWAY.md). Document here anything
project-specific about the mounts and services (what the persistent mount holds,
what each service is).
"""

from setup_common import check_setup_version, make_config
from subtrees.devenv_utils import docker_launch


def main():
    check_setup_version()
    docker_launch(make_config())


if __name__ == "__main__":
    main()
''',
}

# Entry points meant to be run as ./<script>; chmod'd executable when written.
_EXECUTABLE = {"setup_wizard.py", "build_docker_image.py", "run_docker.py"}


def main() -> int:
    wrote, skipped = [], []
    for rel, content in _FILES.items():
        path = REPO_ROOT / rel
        if path.exists():
            skipped.append(rel)
            continue
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content)
        if rel in _EXECUTABLE:
            path.chmod(0o755)
        wrote.append(rel)

    for rel in wrote:
        print(f"  wrote    {rel}")
    for rel in skipped:
        print(f"  skipped  {rel}  (already exists)")

    print("\nNext steps:")
    print("  1. Fill in devenv.toml (project name, ports, versions).")
    print("  2. Write docker-setup/Dockerfile, the image setup_wizard.py builds")
    print("     (crib from an existing consumer).")
    print("  3. Add any project-specific steps to setup_wizard.py's SetupWizard")
    print("     class, then run ./setup_wizard.py.")
    print("  4. Point your CLAUDE.md at subtrees/devenv_utils/WORKFLOW.md and")
    print("     SUBTREES.md (a short workflow section with links suffices).")
    print("  5. Commit. See subtrees/devenv_utils/CONSUMER_SETUP.md for details.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
