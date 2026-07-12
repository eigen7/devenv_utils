#!/usr/bin/env python3
"""Scaffold the thin consumer-side glue for a repo that submodules devenv_utils.

Run from the consumer repo root, AFTER adding the submodule:

    git submodule add https://github.com/eigen7/devenv_utils.git \\
        submodules/devenv_utils
    python3 submodules/devenv_utils/scaffold_consumer.py

It writes the generic glue (never overwriting an existing file):

    submodules/__init__.py    package marker so `submodules.devenv_utils` imports
    submodules/README.md      pointer to the submodule workflow doc
    py/setup_check.py         import bridge for py/ entrypoints
    setup_common.py           project config TEMPLATE (fill in)

Then: fill in setup_common.py, call `tool.setup_git_config()` in your setup
wizard, and point your CLAUDE.md at submodules/devenv_utils/SUBMODULES.md.
See CONSUMER_SETUP.md.
"""
import sys
from pathlib import Path

# This script lives at <repo>/submodules/devenv_utils/scaffold_consumer.py.
REPO_ROOT = Path(__file__).resolve().parents[2]

_FILES = {
    "submodules/__init__.py": '''\
"""Package root for the git submodules under submodules/ (see README.md).

This file exists so submodule packages can be imported as `submodules.<name>`
from the repo root. It is a project-owned file, not part of any submodule.
"""
''',

    "submodules/README.md": '''\
This directory contains git submodules: full checkouts of repos we control,
each pinned to a commit. The workflow -- changing a submodule, pointer-bump
rules, first-clone initialization, worktree interactions -- is documented in
[devenv_utils/SUBMODULES.md](devenv_utils/SUBMODULES.md). Read it before
touching anything under this directory.

A plain `git clone` leaves the submodules empty; the first run of any
host-side script populates them (see the stanza atop setup_common.py).
''',

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

    "setup_common.py": '''\
"""Project-specific devenv configuration for THIS project.

The generic host-side machinery lives in the `submodules/devenv_utils` git
submodule; this module supplies the project-specific DevenvConfig and
constants. It lives at the repo root so host-side scripts can import it
without PYTHONPATH.
"""

import subprocess
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent

# submodules/devenv_utils is a git submodule, so a clone made without
# --recurse-submodules leaves its directory empty. Every host-side entry point
# imports this module before anything else, so populating the submodule here --
# before the imports below -- makes a plain `git clone` just work.
if not (REPO_ROOT / "submodules" / "devenv_utils" / "__init__.py").exists():
    subprocess.run(["git", "submodule", "update", "--init"],
                   cwd=REPO_ROOT, check=True)

from submodules.devenv_utils import (  # noqa: E402
    DevenvConfig,
    DevTool,
    check_setup_version as _check_setup_version,
)

# Bump to force users to rerun the setup wizard (major bump wipes target/).
SETUP_VERSION = "1.0.0"
# Bump when the Dockerfile changes in a way that requires a rebuild.
MINIMUM_REQUIRED_IMAGE_VERSION = "0.0.0"
# Ports forwarded host -> container by run_docker.py.
REQUIRED_PORTS = []


def check_setup_version():
    _check_setup_version(make_config())


def dev_tool() -> DevTool:
    """Return the project's dev-workflow helper (clang-format)."""
    return DevTool(make_config())


def make_config() -> DevenvConfig:
    """Build the DevenvConfig consumed by every host-side script."""
    return DevenvConfig(
        name="CHANGE_ME",
        repo_root=REPO_ROOT,
        required_ports=REQUIRED_PORTS,
        min_image_version=MINIMUM_REQUIRED_IMAGE_VERSION,
        setup_version=SETUP_VERSION,
    )
''',
}


def main() -> int:
    wrote, skipped = [], []
    for rel, content in _FILES.items():
        path = REPO_ROOT / rel
        if path.exists():
            skipped.append(rel)
            continue
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content)
        wrote.append(rel)

    for rel in wrote:
        print(f"  wrote    {rel}")
    for rel in skipped:
        print(f"  skipped  {rel}  (already exists)")

    print("\nNext steps:")
    print("  1. Fill in setup_common.py (project name, ports, versions).")
    print("  2. Call tool.setup_git_config() in your setup wizard so every")
    print("     checkout gets the submodule-sync git settings.")
    print("  3. Point your CLAUDE.md at submodules/devenv_utils/SUBMODULES.md")
    print("     (a short 'Git submodules' section with a link suffices).")
    print("  4. Commit. See submodules/devenv_utils/CONSUMER_SETUP.md for details.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
